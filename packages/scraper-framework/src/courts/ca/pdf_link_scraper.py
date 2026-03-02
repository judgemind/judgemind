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
from urllib.parse import urljoin

import httpx
import pdfplumber
import structlog
from bs4 import BeautifulSoup

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig

logger = structlog.get_logger(__name__)


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

    # Case number regex applied to extracted PDF text
    case_number_re: re.Pattern = field(default_factory=lambda: re.compile(r"\b\d{2}-\d{8}\b"))


class PdfLinkScraper(BaseScraper):
    """Scrapes Pattern-2 courts: PDF links on an index page, one PDF per judge."""

    def __init__(
        self,
        config: ScraperConfig,
        pdf_config: PdfLinkConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._pdf_config = pdf_config

    def fetch_documents(self) -> list[CapturedDocument]:
        pc = self._pdf_config
        docs = []

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

            for href, link_text in links:
                time.sleep(self.config.request_delay_seconds)
                try:
                    doc = self._fetch_one_pdf(client, href, link_text)
                    docs.append(doc)
                    self._log.debug(
                        "Fetched PDF",
                        judge=doc.judge_name,
                        dept=doc.department,
                        url=href,
                    )
                except Exception as exc:
                    self._log.error(
                        "Failed to fetch PDF",
                        url=href,
                        link_text=link_text,
                        error=str(exc),
                    )

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
            raw_name = gd.get("judge_name", "").strip()
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
