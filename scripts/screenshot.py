#!/usr/bin/env python3
"""Take a screenshot of a page on dev.judgemind.org.

Usage:
    python3 scripts/screenshot.py /rulings
    python3 scripts/screenshot.py /rulings --output tmp/rulings.png
    python3 scripts/screenshot.py /cases/123 --full-page
    python3 scripts/screenshot.py /rulings --selector ".ruling-card"
    python3 scripts/screenshot.py /rulings --width 1280 --height 720
    python3 scripts/screenshot.py /rulings --wait 5000
"""

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOST = "dev.judgemind.org"
BASE_URL = f"https://{ALLOWED_HOST}"


def validate_url(path: str) -> str:
    """Ensure the URL points to dev.judgemind.org and nothing else."""
    if path.startswith("http://") or path.startswith("https://"):
        parsed = urlparse(path)
        if parsed.hostname != ALLOWED_HOST:
            print(f"ERROR: Only {ALLOWED_HOST} URLs are allowed, got: {parsed.hostname}", file=sys.stderr)
            sys.exit(1)
        return path
    # Treat as a path relative to the base URL
    if not path.startswith("/"):
        path = "/" + path
    return BASE_URL + path


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Screenshot a page on {ALLOWED_HOST}")
    parser.add_argument("path", help="URL path (e.g. /rulings) or full URL on dev.judgemind.org")
    parser.add_argument("--output", "-o", help="Output file path (default: tmp/screenshot.png)")
    parser.add_argument("--full-page", action="store_true", help="Capture the full scrollable page")
    parser.add_argument("--selector", "-s", help="CSS selector to screenshot a specific element")
    parser.add_argument("--width", type=int, default=1280, help="Viewport width (default: 1280)")
    parser.add_argument("--height", type=int, default=720, help="Viewport height (default: 720)")
    parser.add_argument("--wait", type=int, default=3000, help="Wait time in ms after load for JS rendering (default: 3000)")
    args = parser.parse_args()

    url = validate_url(args.path)

    # Determine output path
    if args.output:
        output = Path(args.output)
    else:
        output = Path("tmp/screenshot.png")
    output.parent.mkdir(parents=True, exist_ok=True)

    # Import playwright here so the argparse help works without it installed
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright is not installed. Run: pip install playwright && playwright install chromium", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})

        print(f"Navigating to {url} ...")
        page.goto(url, wait_until="networkidle")

        # Extra wait for client-side JS rendering (React hydration, data fetching)
        if args.wait > 0:
            page.wait_for_timeout(args.wait)

        if args.selector:
            element = page.query_selector(args.selector)
            if element is None:
                print(f"ERROR: selector '{args.selector}' not found on page", file=sys.stderr)
                browser.close()
                sys.exit(1)
            element.screenshot(path=str(output))
        else:
            page.screenshot(path=str(output), full_page=args.full_page)

        browser.close()

    abs_path = output.resolve()
    print(f"Screenshot saved to {abs_path}")


if __name__ == "__main__":
    main()
