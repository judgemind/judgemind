#!/usr/bin/env python3
"""Take a screenshot of a page on dev.judgemind.org.

Usage:
    python3 scripts/screenshot.py /rulings
    python3 scripts/screenshot.py /rulings --output tmp/rulings.png
    python3 scripts/screenshot.py /cases/123 --full-page
    python3 scripts/screenshot.py /rulings --selector ".ruling-card"
    python3 scripts/screenshot.py /rulings --width 1280 --height 720
    python3 scripts/screenshot.py /rulings --wait 5000

The script auto-bootstraps its own venv with playwright and chromium on first
run. No manual setup is required. The venv lives at ~/.judgemind/tools-venv/
and is reused across sessions and worktrees.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOST = "dev.judgemind.org"
BASE_URL = f"https://{ALLOWED_HOST}"
TOOLS_VENV_DIR = Path.home() / ".judgemind" / "tools-venv"


def _get_venv_python() -> str:
    """Return the path to the Python executable inside the tools venv."""
    return str(TOOLS_VENV_DIR / "bin" / "python3")


def _ensure_venv() -> None:
    """Create the tools venv and install playwright + chromium if needed."""
    venv_python = _get_venv_python()

    if Path(venv_python).exists():
        # Venv exists — check if playwright is importable
        result = subprocess.run(
            [venv_python, "-c", "import playwright"],
            capture_output=True,
        )
        if result.returncode == 0:
            return  # All good — venv and playwright are ready

    # Create or repair the venv
    print("Auto-bootstrapping tools venv at", TOOLS_VENV_DIR, "...", file=sys.stderr)
    TOOLS_VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "venv", str(TOOLS_VENV_DIR)],
        check=True,
    )

    venv_pip = str(TOOLS_VENV_DIR / "bin" / "pip")
    subprocess.run(
        [venv_pip, "install", "--quiet", "playwright"],
        check=True,
    )

    venv_playwright = str(TOOLS_VENV_DIR / "bin" / "playwright")
    subprocess.run(
        [venv_playwright, "install", "chromium"],
        check=True,
    )

    print("Tools venv ready.", file=sys.stderr)


def _reexec_in_venv() -> None:
    """Re-execute this script inside the tools venv if we are not already in it."""
    venv_python = _get_venv_python()

    # If we are already running inside the tools venv, nothing to do
    if os.path.realpath(sys.executable) == os.path.realpath(venv_python):
        return

    # Ensure venv exists and is set up
    _ensure_venv()

    # Re-exec ourselves with the venv Python, forwarding all arguments
    os.execv(venv_python, [venv_python, *sys.argv])


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
    # Auto-bootstrap: ensure we are running inside the tools venv with playwright
    _reexec_in_venv()

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

    # Import playwright — guaranteed available since _reexec_in_venv() ensured it
    from playwright.sync_api import sync_playwright

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
