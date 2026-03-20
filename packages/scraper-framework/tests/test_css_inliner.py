"""Tests for the CSS inliner utility."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from framework.css_inliner import inline_css

# ---------------------------------------------------------------------------
# Basic inlining
# ---------------------------------------------------------------------------


def test_inline_css_replaces_link_tag_with_style_block() -> None:
    """A <link rel='stylesheet'> should be replaced with an inline <style> block."""
    html = (
        b"<html><head>"
        b'<link rel="stylesheet" href="/css/styles.css">'
        b"</head><body><p>Hello</p></body></html>"
    )
    css_content = b"body { color: red; }"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = css_content
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = mock_response

    base = "https://court.example.com/rulings/page.html"
    result = inline_css(html, base_url=base, http_client=mock_client)

    assert b"<style>" in result
    assert b"body { color: red; }" in result
    assert b'<link rel="stylesheet"' not in result


def test_inline_css_preserves_existing_style_tags() -> None:
    """Existing <style> blocks should remain untouched."""
    html = (
        b"<html><head><style>h1 { font-size: 20px; }</style></head><body><p>Hello</p></body></html>"
    )
    result = inline_css(html, base_url="https://court.example.com/page.html")

    assert b"h1 { font-size: 20px; }" in result


def test_inline_css_resolves_relative_urls() -> None:
    """Relative CSS URLs should be resolved against the base URL."""
    html = (
        b"<html><head>"
        b'<link rel="stylesheet" href="../css/main.css">'
        b"</head><body><p>Hello</p></body></html>"
    )
    css_content = b".main { margin: 0; }"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = css_content
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = mock_response

    base = "https://court.example.com/rulings/page.html"
    result = inline_css(html, base_url=base, http_client=mock_client)

    # Should have resolved to https://court.example.com/css/main.css
    mock_client.get.assert_called_once_with(
        "https://court.example.com/css/main.css",
        timeout=10.0,
    )
    assert b".main { margin: 0; }" in result


def test_inline_css_handles_multiple_stylesheets() -> None:
    """Multiple <link> tags should all be inlined."""
    html = (
        b"<html><head>"
        b'<link rel="stylesheet" href="/css/a.css">'
        b'<link rel="stylesheet" href="/css/b.css">'
        b"</head><body><p>Hello</p></body></html>"
    )

    responses = {
        "https://court.example.com/css/a.css": b"a { color: blue; }",
        "https://court.example.com/css/b.css": b"b { color: green; }",
    }

    def mock_get(url: str, timeout: float = 10.0) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.content = responses.get(url, b"")
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = mock_get

    base = "https://court.example.com/page.html"
    result = inline_css(html, base_url=base, http_client=mock_client)

    assert b"a { color: blue; }" in result
    assert b"b { color: green; }" in result
    assert mock_client.get.call_count == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_inline_css_no_stylesheets() -> None:
    """HTML without any CSS references should be returned unchanged."""
    html = b"<html><head><title>Test</title></head><body><p>Hello</p></body></html>"
    result = inline_css(html, base_url="https://court.example.com/page.html")

    # Content should be equivalent (parser may normalize whitespace)
    assert b"<p>Hello</p>" in result


def test_inline_css_graceful_on_fetch_failure() -> None:
    """Failed CSS fetches should log a warning and continue, not raise."""
    html = (
        b"<html><head>"
        b'<link rel="stylesheet" href="/css/broken.css">'
        b'<link rel="stylesheet" href="/css/working.css">'
        b"</head><body><p>Hello</p></body></html>"
    )

    call_count = 0

    def mock_get(url: str, timeout: float = 10.0) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if "broken" in url:
            raise httpx.ConnectError("Connection refused")
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b".working { display: block; }"
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = mock_get

    base = "https://court.example.com/page.html"
    result = inline_css(html, base_url=base, http_client=mock_client)

    # The working CSS should be inlined
    assert b".working { display: block; }" in result
    # The broken one should not have caused a crash
    assert call_count == 2


def test_inline_css_graceful_on_http_error() -> None:
    """HTTP error responses (404, 500) should be handled gracefully."""
    html = (
        b"<html><head>"
        b'<link rel="stylesheet" href="/css/missing.css">'
        b"</head><body><p>Hello</p></body></html>"
    )

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_response
    )

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = mock_response

    # Should not raise
    base = "https://court.example.com/page.html"
    result = inline_css(html, base_url=base, http_client=mock_client)
    assert b"<p>Hello</p>" in result


def test_inline_css_skips_non_stylesheet_links() -> None:
    """<link> tags that aren't stylesheets should not be affected."""
    html = (
        b"<html><head>"
        b'<link rel="icon" href="/favicon.ico">'
        b'<link rel="stylesheet" href="/css/styles.css">'
        b"</head><body><p>Hello</p></body></html>"
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"body { margin: 0; }"
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = mock_response

    base = "https://court.example.com/page.html"
    result = inline_css(html, base_url=base, http_client=mock_client)

    # Only the stylesheet link should have been fetched
    mock_client.get.assert_called_once()
    assert b"body { margin: 0; }" in result
    # Icon link should still be present
    assert b"favicon.ico" in result


def test_inline_css_no_http_client_creates_one() -> None:
    """When no HTTP client is provided, one should be created internally."""
    html = (
        b"<html><head>"
        b'<link rel="stylesheet" href="/css/styles.css">'
        b"</head><body><p>Hello</p></body></html>"
    )

    # Patch httpx.Client to avoid real HTTP calls
    with patch("framework.css_inliner.httpx.Client") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"p { color: black; }"
        mock_response.raise_for_status = MagicMock()

        mock_instance = MagicMock()
        mock_instance.get.return_value = mock_response
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_instance

        result = inline_css(html, base_url="https://court.example.com/page.html")

        assert b"p { color: black; }" in result


def test_inline_css_handles_non_html_gracefully() -> None:
    """Non-HTML content or garbage bytes should be returned as-is."""
    garbage = b"\x00\x01\x02 not html at all"
    result = inline_css(garbage, base_url="https://example.com")
    assert result == garbage


def test_inline_css_handles_empty_bytes() -> None:
    """Empty input should return empty output."""
    result = inline_css(b"", base_url="https://example.com")
    assert result == b""


def test_inline_css_large_css_size_limit() -> None:
    """CSS files larger than the size limit should be skipped."""
    html = (
        b"<html><head>"
        b'<link rel="stylesheet" href="/css/huge.css">'
        b"</head><body><p>Hello</p></body></html>"
    )

    # Create a response that's 2MB (larger than reasonable limit)
    huge_css = b"x" * (2 * 1024 * 1024)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = huge_css
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.return_value = mock_response

    result = inline_css(
        html,
        base_url="https://court.example.com/page.html",
        http_client=mock_client,
        max_css_size_bytes=1024 * 1024,
    )

    # The huge CSS should NOT be inlined
    assert b"x" * 100 not in result


# ---------------------------------------------------------------------------
# Integration with BaseScraper
# ---------------------------------------------------------------------------


def test_base_scraper_inlines_css_for_html_docs() -> None:
    """BaseScraper._process_document should inline CSS for HTML-format docs."""
    from datetime import datetime

    from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig

    html_with_css = (
        b"<html><head>"
        b'<link rel="stylesheet" href="https://court.example.com/css/styles.css">'
        b"</head><body><p>Ruling text</p></body></html>"
    )

    config = ScraperConfig(
        scraper_id="test",
        state="CA",
        county="Test",
        court="Superior Court",
        target_urls=["https://court.example.com"],
    )
    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://court.example.com/rulings/page.html",
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=html_with_css,
        content_hash="",
    )

    class TestScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            return [doc]

        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            return d

    # Mock the CSS inliner to verify it's called
    with patch("framework.base.inline_css") as mock_inline:
        mock_inline.return_value = (
            b"<html><head><style>body{}</style></head><body><p>Ruling text</p></body></html>"
        )
        scraper = TestScraper(config=config)
        scraper.run()
        mock_inline.assert_called_once()
        call_args = mock_inline.call_args
        assert call_args[0][0] == html_with_css
        assert call_args[1]["base_url"] == "https://court.example.com/rulings/page.html"


def test_base_scraper_does_not_inline_css_for_pdf_docs() -> None:
    """BaseScraper._process_document should NOT inline CSS for PDF-format docs."""
    from datetime import datetime

    from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig

    config = ScraperConfig(
        scraper_id="test",
        state="CA",
        county="Test",
        court="Superior Court",
        target_urls=["https://court.example.com"],
    )
    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://court.example.com/rulings/file.pdf",
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.PDF,
        raw_content=b"%PDF-1.4 fake pdf content",
        content_hash="",
    )

    class TestScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            return [doc]

        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            return d

    with patch("framework.base.inline_css") as mock_inline:
        scraper = TestScraper(config=config)
        scraper.run()
        mock_inline.assert_not_called()


def test_base_scraper_continues_if_css_inlining_fails() -> None:
    """If CSS inlining raises, the document should still be processed."""
    from datetime import datetime

    from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
    from framework.hashing import sha256_hex

    html = b"<html><body><p>Ruling</p></body></html>"

    config = ScraperConfig(
        scraper_id="test",
        state="CA",
        county="Test",
        court="Superior Court",
        target_urls=["https://court.example.com"],
    )
    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://court.example.com/page.html",
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=html,
        content_hash="",
    )

    captured: list[CapturedDocument] = []

    class TestScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            return [doc]

        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            captured.append(d)
            return d

    with patch("framework.base.inline_css", side_effect=Exception("CSS inlining crashed")):
        scraper = TestScraper(config=config)
        health = scraper.run()

        # Should still succeed — CSS inlining failure is non-fatal
        assert health.success is True
        assert health.records_captured == 1
        assert len(captured) == 1
        # Content hash should be based on original content
        assert captured[0].content_hash == sha256_hex(html)
