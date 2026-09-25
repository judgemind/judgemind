"""Browser automation utilities for Playwright-based scrapers."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

import structlog

logger = structlog.get_logger(__name__)


def playwright_proxy_settings(proxy_url: str) -> dict[str, str]:
    """Convert a ``scheme://user:pass@host:port`` proxy URL to Playwright's proxy dict.

    Playwright/Chromium ignore credentials embedded in ``proxy.server``: the
    CONNECT goes out unauthenticated, the proxy answers ``407`` and Chromium
    surfaces it as an opaque ``net::ERR_TIMED_OUT`` (verified against the live
    Bright Data zone, #4668). Credentials must be passed as separate
    ``username`` / ``password`` fields. URL-encoded credentials are decoded.
    """
    parts = urlsplit(proxy_url)
    if not parts.hostname:
        return {"server": proxy_url}
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    server = f"{parts.scheme or 'http'}://{host}"
    if parts.port:
        server = f"{server}:{parts.port}"
    settings = {"server": server}
    if parts.username:
        settings["username"] = unquote(parts.username)
        settings["password"] = unquote(parts.password or "")
    return settings


async def apply_stealth(page: Any) -> None:
    """Apply playwright-stealth evasions to a page to avoid bot detection.

    Uses the ``playwright-stealth`` library to mask browser automation
    fingerprints (WebGL, navigator properties, plugins, etc.) that
    anti-bot services (Cloudflare, Turnstile) use for detection.

    Falls back gracefully if playwright-stealth is not installed, applying
    only the minimal webdriver override.

    Args:
        page: The Playwright page object.
    """
    try:
        from playwright_stealth import Stealth

        stealth = Stealth()
        await stealth.apply_stealth_async(page)
    except ImportError:
        logger.warning("playwright-stealth not installed, using minimal stealth only")
        # Minimal fallback: remove webdriver flag
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
