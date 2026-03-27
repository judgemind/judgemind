#!/usr/bin/env python3
# venv: scraper-framework
"""Take a screenshot of a page on dev.judgemind.org.

Usage:
    scripts/run-py.sh scripts/screenshot.py /rulings
    scripts/run-py.sh scripts/screenshot.py /rulings --output tmp/rulings.png
    scripts/run-py.sh scripts/screenshot.py /cases/123 --full-page
    scripts/run-py.sh scripts/screenshot.py /rulings --selector ".ruling-card"
    scripts/run-py.sh scripts/screenshot.py /rulings --width 1280 --height 720
    scripts/run-py.sh scripts/screenshot.py /rulings --wait 5000
    scripts/run-py.sh scripts/screenshot.py --auth /admin/data-quality --output tmp/dq.png

Requires the scraper-framework venv (which includes playwright). Run via
scripts/run-py.sh, which reads the ``# venv:`` header and activates the
correct venv automatically.

After creating the venv for the first time, install chromium for playwright::

    packages/scraper-framework/.venv/bin/playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.sync_api import Page

ALLOWED_HOST = "dev.judgemind.org"
BASE_URL = f"https://{ALLOWED_HOST}"
SECRET_ID = "judgemind/dev/agent-admin"
AWS_REGION = "us-west-2"


def validate_url(path: str) -> str:
    """Ensure the URL points to dev.judgemind.org and nothing else."""
    if path.startswith("http://") or path.startswith("https://"):
        parsed = urlparse(path)
        if parsed.hostname != ALLOWED_HOST:
            print(
                f"ERROR: Only {ALLOWED_HOST} URLs are allowed, got: {parsed.hostname}",
                file=sys.stderr,
            )
            sys.exit(1)
        return path
    # Treat as a path relative to the base URL
    if not path.startswith("/"):
        path = "/" + path
    return BASE_URL + path


def fetch_credentials() -> tuple[str, str]:
    """Fetch admin credentials from AWS Secrets Manager.

    Returns:
        A tuple of (email, password).

    Raises:
        KeyError: If the secret does not contain ``email`` or ``password`` keys.
    """
    import boto3  # lazy import — only needed when --auth is used

    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    response = client.get_secret_value(SecretId=SECRET_ID)
    secret = json.loads(response["SecretString"])
    # Validate required keys are present — raise KeyError if missing
    email = secret["email"]
    password = secret["password"]
    return email, password


def perform_login(page: Page, email: str, password: str) -> None:
    """Log in to dev.judgemind.org via the web login form.

    Navigates to the login page, fills in email/password, submits the form,
    and waits for the post-login redirect. After this returns, the page's
    browser context holds the auth cookies for subsequent navigation.

    Args:
        page: A Playwright Page object.
        email: The login email address.
        password: The login password.

    Raises:
        TimeoutError: If the login redirect does not complete in time.
    """
    login_url = f"{BASE_URL}/auth/login"
    print(f"Authenticating via {login_url} ...")

    page.goto(login_url, wait_until="networkidle")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')

    # Wait for the post-login redirect (the login page navigates to / on success).
    # Use a glob pattern to accept any path on the allowed host after login.
    page.wait_for_url(f"{BASE_URL}/**", timeout=15000)
    print("Authentication successful.")


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Screenshot a page on {ALLOWED_HOST}")
    parser.add_argument(
        "path", help="URL path (e.g. /rulings) or full URL on dev.judgemind.org"
    )
    parser.add_argument(
        "--output", "-o", help="Output file path (default: tmp/screenshot.png)"
    )
    parser.add_argument(
        "--full-page", action="store_true", help="Capture the full scrollable page"
    )
    parser.add_argument(
        "--selector", "-s", help="CSS selector to screenshot a specific element"
    )
    parser.add_argument(
        "--width", type=int, default=1280, help="Viewport width (default: 1280)"
    )
    parser.add_argument(
        "--height", type=int, default=720, help="Viewport height (default: 720)"
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=3000,
        help="Wait time in ms after load for JS rendering (default: 3000)",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Log in as admin before taking the screenshot (fetches credentials from AWS Secrets Manager)",
    )
    args = parser.parse_args()

    url = validate_url(args.path)

    # Determine output path
    if args.output:
        output = Path(args.output)
    else:
        output = Path("tmp/screenshot.png")
    output.parent.mkdir(parents=True, exist_ok=True)

    # Fetch credentials before launching the browser if auth is requested
    creds: tuple[str, str] | None = None
    if args.auth:
        creds = fetch_credentials()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})

        # If auth requested, log in first so the browser context has auth cookies
        if creds is not None:
            perform_login(page, creds[0], creds[1])

        print(f"Navigating to {url} ...")
        page.goto(url, wait_until="networkidle")

        # Extra wait for client-side JS rendering (React hydration, data fetching)
        if args.wait > 0:
            page.wait_for_timeout(args.wait)

        if args.selector:
            element = page.query_selector(args.selector)
            if element is None:
                print(
                    f"ERROR: selector '{args.selector}' not found on page",
                    file=sys.stderr,
                )
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
