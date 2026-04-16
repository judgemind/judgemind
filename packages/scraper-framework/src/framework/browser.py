"""Browser automation utilities for Playwright-based scrapers."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


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
