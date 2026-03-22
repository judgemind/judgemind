"""CSS inliner — fetches external stylesheets and inlines them into HTML.

Makes archived HTML documents self-contained so they render correctly
when viewed directly (e.g. via "Download original" presigned URLs).

Usage:
    from framework.css_inliner import inline_css

    # With an existing HTTP client (preferred — reuses connections):
    inlined = inline_css(html_bytes, base_url=page_url, http_client=client)

    # Without a client (creates one internally):
    inlined = inline_css(html_bytes, base_url=page_url)
"""

from __future__ import annotations

from urllib.parse import urljoin

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

logger = structlog.get_logger(__name__)

# Default maximum size for a single CSS file (1 MB).
# Anything larger is likely not a stylesheet (or is bloated beyond usefulness).
_DEFAULT_MAX_CSS_SIZE = 1024 * 1024

# Timeout for fetching individual CSS files (seconds).
_CSS_FETCH_TIMEOUT = 10.0


def inline_css(
    html_bytes: bytes,
    base_url: str,
    http_client: httpx.Client | None = None,
    max_css_size_bytes: int = _DEFAULT_MAX_CSS_SIZE,
) -> bytes:
    """Fetch external CSS referenced in *html_bytes* and inline it as ``<style>`` blocks.

    Parameters
    ----------
    html_bytes:
        Raw HTML content as bytes.
    base_url:
        The URL of the HTML page, used to resolve relative CSS URLs.
    http_client:
        Optional ``httpx.Client`` to reuse for fetching CSS. If not provided,
        a temporary client is created (and closed) internally.
    max_css_size_bytes:
        Maximum size in bytes for a single CSS file. Larger files are skipped
        with a warning log.

    Returns
    -------
    bytes
        The HTML with external stylesheets inlined. If the input is not valid
        HTML or has no ``<link rel="stylesheet">`` tags, it is returned unchanged.

    Notes
    -----
    - Existing ``<style>`` blocks are preserved unchanged.
    - CSS fetch failures are logged as warnings and skipped (partial styling
      is better than no styling).
    - Non-HTML input (empty bytes, binary data) is returned as-is.
    """
    if not html_bytes:
        return html_bytes

    # Quick check: if it doesn't look like HTML at all, return as-is
    try:
        html_str = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return html_bytes

    if "<" not in html_str:
        return html_bytes

    # Fast path: skip the expensive BeautifulSoup parse when the HTML
    # doesn't reference any external stylesheets.  This saves ~0.5 ms
    # per call, which matters when processing hundreds of HTML documents
    # per scraper run (e.g. LA tentatives: 194 docs × 2 parses each).
    if "stylesheet" not in html_str:
        return html_bytes

    soup = BeautifulSoup(html_str, "lxml")

    # Find all <link rel="stylesheet"> tags
    link_tags = soup.find_all("link", rel=lambda v: v and "stylesheet" in v)
    if not link_tags:
        # No external stylesheets to inline — return original bytes unchanged
        return html_bytes

    if http_client is not None:
        _inline_links(soup, link_tags, base_url, http_client, max_css_size_bytes)
    else:
        with httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            _inline_links(soup, link_tags, base_url, client, max_css_size_bytes)

    return soup.encode(formatter="minimal")


def _inline_links(
    soup: BeautifulSoup,
    link_tags: list[Tag],
    base_url: str,
    client: httpx.Client,
    max_css_size_bytes: int,
) -> None:
    """Replace ``<link rel="stylesheet">`` tags with inline ``<style>`` blocks."""
    for link in link_tags:
        href = link.get("href")
        if not href:
            continue

        # Resolve relative URL against the page's base URL
        absolute_url = urljoin(base_url, str(href))

        try:
            css_bytes = _fetch_css(client, absolute_url, max_css_size_bytes)
        except _CSSFetchError as exc:
            logger.warning(
                "Failed to fetch CSS for inlining",
                url=absolute_url,
                reason=str(exc),
            )
            continue

        if css_bytes is None:
            # Skipped (e.g. too large)
            continue

        # Decode CSS content
        try:
            css_text = css_bytes.decode("utf-8", errors="replace")
        except Exception:
            logger.warning("Failed to decode CSS content", url=absolute_url)
            continue

        # Create a new <style> tag and replace the <link>
        style_tag = soup.new_tag("style")
        style_tag.string = css_text
        link.replace_with(style_tag)


class _CSSFetchError(Exception):
    """Internal error for CSS fetch failures."""


def _fetch_css(
    client: httpx.Client,
    url: str,
    max_size_bytes: int,
) -> bytes | None:
    """Fetch a CSS file from *url*. Returns bytes or None if skipped.

    Raises ``_CSSFetchError`` on network/HTTP errors.
    """
    try:
        response = client.get(url, timeout=_CSS_FETCH_TIMEOUT)
        response.raise_for_status()
    except (httpx.HTTPError, httpx.StreamError) as exc:
        raise _CSSFetchError(str(exc)) from exc

    content = response.content
    if len(content) > max_size_bytes:
        logger.warning(
            "CSS file too large, skipping",
            url=url,
            size_bytes=len(content),
            max_bytes=max_size_bytes,
        )
        return None

    return content
