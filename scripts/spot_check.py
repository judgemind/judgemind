#!/usr/bin/env python3
# venv: scraper-framework
"""Spot-check case detail pages for data quality issues.

Takes screenshots of multiple case detail pages from the rulings feed
for manual review. Useful for verifying scraper output, field extraction,
and frontend rendering of ruling records.

Usage:
    scripts/run-py.sh scripts/spot_check.py                      # Screenshot 10 cases
    scripts/run-py.sh scripts/spot_check.py --count 20           # Screenshot 20 cases
    scripts/run-py.sh scripts/spot_check.py --output tmp/qa      # Custom output directory
    scripts/run-py.sh scripts/spot_check.py --base-url https://dev.judgemind.org

Requires the scraper-framework venv (which includes playwright). Run via
scripts/run-py.sh, which reads the ``# venv:`` header and activates the
correct venv automatically.

After creating the venv for the first time, install chromium for playwright::

    packages/scraper-framework/.venv/bin/playwright install chromium
"""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOSTS = {"dev.judgemind.org", "judgemind.org", "localhost"}
DEFAULT_BASE_URL = "https://dev.judgemind.org"


def validate_base_url(url: str) -> str:
    """Ensure the base URL points to an allowed host."""
    parsed = urlparse(url)
    if parsed.hostname not in ALLOWED_HOSTS:
        print(
            f"ERROR: Only {ALLOWED_HOSTS} are allowed, got: {parsed.hostname}",
            file=sys.stderr,
        )
        sys.exit(1)
    return url.rstrip("/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spot-check case detail pages for data quality issues"
    )
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=10,
        help="Number of cases to screenshot (default: 10)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="tmp/spot-check",
        help="Output directory for screenshots (default: tmp/spot-check)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1400,
        help="Viewport width (default: 1400)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=900,
        help="Viewport height (default: 900)",
    )
    args = parser.parse_args()

    base_url = validate_base_url(args.base_url)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})

        # Get case IDs from the rulings page
        rulings_url = f"{base_url}/rulings"
        print(f"Loading rulings feed: {rulings_url}")
        page.goto(rulings_url, wait_until="networkidle")
        page.wait_for_timeout(5000)

        # Extract all case links
        links = page.eval_on_selector_all(
            'a[href^="/cases/"]',
            "els => els.map(el => ({href: el.getAttribute('href'), text: el.textContent.trim()}))",
        )

        # Deduplicate by href
        seen: set[str] = set()
        unique_links: list[dict[str, str]] = []
        for link in links:
            if link["href"] not in seen:
                seen.add(link["href"])
                unique_links.append(link)

        print(f"Found {len(unique_links)} unique case links")
        for i, link in enumerate(unique_links[: args.count + 10]):
            print(f"  {i + 1}. {link['href']} — {link['text'][:60]}")

        # Screenshot case detail pages
        cases_to_check = unique_links[: args.count]
        for i, link in enumerate(cases_to_check):
            case_url = f"{base_url}{link['href']}"
            case_id = link["href"].split("/cases/")[1]
            short_id = case_id[:8]

            print(f"\nScreenshotting case {i + 1}/{len(cases_to_check)}: {case_url}")
            page.goto(case_url, wait_until="networkidle")
            page.wait_for_timeout(3000)

            # Click all "Show more" buttons to expand ruling text
            for btn in page.query_selector_all("text=Show more"):
                btn.click()
                page.wait_for_timeout(300)
            page.wait_for_timeout(500)

            # Top of page — case header, parties, judges, ruling table
            page.screenshot(path=str(output_dir / f"case-{short_id}-top.png"))

            # Scroll positions to capture ruling text at different depths
            for label, scroll_y in [("mid", 600), ("deep", 1200), ("bottom", 2400)]:
                page.evaluate(f"window.scrollTo(0, {scroll_y})")
                page.wait_for_timeout(300)
                page.screenshot(
                    path=str(output_dir / f"case-{short_id}-{label}.png")
                )

            print(f"  Saved 4 screenshots for {short_id}")

        browser.close()
        print(f"\nAll screenshots saved to {output_dir.resolve()}")


if __name__ == "__main__":
    main()
