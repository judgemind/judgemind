"""Example scraper — demonstrates the BaseScraper pattern with a dummy court.

This is not a real scraper. It shows how to implement BaseScraper correctly
and is used as a reference when building per-court scrapers.
"""

from __future__ import annotations

import time

import httpx

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig


class ExampleScraper(BaseScraper):
    """Minimal BaseScraper implementation for illustration purposes."""

    def fetch_documents(self) -> list[CapturedDocument]:
        docs = []
        for url in self.config.target_urls:
            time.sleep(self.config.request_delay_seconds)
            response = httpx.get(url, timeout=self.config.request_timeout_seconds)
            response.raise_for_status()
            doc = self._make_base_doc(
                source_url=url,
                raw_content=response.content,
                content_format=ContentFormat.HTML,
            )
            docs.append(doc)
        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        # In a real scraper: use BeautifulSoup / pdfplumber / regex to extract fields.
        # Return doc with whatever fields you can populate; never raise.
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(doc.raw_content, "lxml")
            doc.ruling_text = soup.get_text(separator="\n", strip=True)[:2000]
        except Exception:
            pass
        return doc


# ---------------------------------------------------------------------------
# Example usage / smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config = ScraperConfig(
        scraper_id="ca-example-demo",
        state="CA",
        county="Example",
        court="Superior Court",
        target_urls=["https://httpbin.org/html"],
        request_delay_seconds=0.5,
    )
    scraper = ExampleScraper(config=config)
    health = scraper.run()
    print(f"Success: {health.success}, records: {health.records_captured}")
