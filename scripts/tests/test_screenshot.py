"""Tests for screenshot.py --auth flag functionality."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screenshot import fetch_credentials, perform_login, validate_url


def _make_mock_playwright() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Create mock playwright modules and return (mock_sync_playwright, mock_page, mock_browser).

    Injects mock modules into sys.modules so ``from playwright.sync_api import
    sync_playwright`` succeeds even when playwright is not installed (e.g. in CI).
    """
    mock_page = MagicMock()
    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page

    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser

    mock_sync_pw_fn = MagicMock()
    mock_sync_pw_fn.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    mock_sync_pw_fn.return_value.__exit__ = MagicMock(return_value=False)

    return mock_sync_pw_fn, mock_page, mock_browser


class TestValidateUrl:
    """Tests for the existing validate_url function."""

    def test_path_only(self) -> None:
        result = validate_url("/rulings")
        assert result == "https://dev.judgemind.org/rulings"

    def test_path_without_slash(self) -> None:
        result = validate_url("rulings")
        assert result == "https://dev.judgemind.org/rulings"

    def test_full_allowed_url(self) -> None:
        result = validate_url("https://dev.judgemind.org/admin/data-quality")
        assert result == "https://dev.judgemind.org/admin/data-quality"

    def test_disallowed_host_exits(self) -> None:
        with pytest.raises(SystemExit):
            validate_url("https://evil.example.com/admin")


class TestFetchCredentials:
    """Tests for fetch_credentials (boto3 secret fetching)."""

    def test_returns_email_and_password(self) -> None:
        secret_value = json.dumps({"email": "test@example.com", "password": "s3cret"})
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": secret_value}
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            email, password = fetch_credentials()

        assert email == "test@example.com"
        assert password == "s3cret"
        mock_boto3.client.assert_called_once_with(
            "secretsmanager", region_name="us-west-2"
        )
        mock_client.get_secret_value.assert_called_once_with(
            SecretId="judgemind/dev/agent-admin"
        )

    def test_missing_email_key_raises(self) -> None:
        secret_value = json.dumps({"password": "s3cret"})
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": secret_value}
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            with pytest.raises(KeyError, match="email"):
                fetch_credentials()

    def test_missing_password_key_raises(self) -> None:
        secret_value = json.dumps({"email": "test@example.com"})
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": secret_value}
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            with pytest.raises(KeyError, match="password"):
                fetch_credentials()


class TestPerformLogin:
    """Tests for perform_login (Playwright login flow)."""

    def test_fills_form_and_submits(self) -> None:
        mock_page = MagicMock()
        # Simulate successful navigation after login
        mock_page.url = "https://dev.judgemind.org/"

        perform_login(mock_page, "admin@example.com", "p@ssw0rd")

        mock_page.goto.assert_called_once_with(
            "https://dev.judgemind.org/auth/login", wait_until="networkidle"
        )
        mock_page.fill.assert_any_call('input[name="email"]', "admin@example.com")
        mock_page.fill.assert_any_call('input[name="password"]', "p@ssw0rd")
        mock_page.click.assert_called_once_with('button[type="submit"]')
        mock_page.wait_for_url.assert_called_once()

    def test_raises_on_login_failure(self) -> None:
        mock_page = MagicMock()
        # Simulate timeout waiting for URL change
        mock_page.wait_for_url.side_effect = TimeoutError("Navigation timeout")

        with pytest.raises(TimeoutError):
            perform_login(mock_page, "admin@example.com", "wrong")

    def test_wait_for_url_pattern(self) -> None:
        """Verify the URL pattern used for post-login redirect detection.

        Regression test for #2760: the previous ``{BASE_URL}/**`` glob also
        matched ``/auth/login`` itself, so ``wait_for_url`` returned
        immediately without actually waiting for the post-login redirect,
        causing the caller to navigate away before the refresh-token cookie
        was set. The fixed pattern is an exact match on the home page URL,
        which is where the login page's ``router.push('/')`` sends the user.
        """
        mock_page = MagicMock()
        perform_login(mock_page, "a@b.com", "pw")

        args, kwargs = mock_page.wait_for_url.call_args
        # Must wait for exactly the home page, not a glob that also matches
        # the login page itself.
        assert args[0] == "https://dev.judgemind.org/"
        assert "**" not in args[0]
        assert kwargs.get("timeout") == 15000


class TestMainAuthIntegration:
    """Tests for the --auth flag in the main function.

    These tests inject mock playwright modules via ``sys.modules`` so the lazy
    ``from playwright.sync_api import sync_playwright`` inside ``main()`` works
    even when playwright is not installed (as in the CI scripts-tests job).
    """

    def test_auth_flag_triggers_login(self) -> None:
        """Verify that --auth triggers credential fetch and login."""
        from screenshot import main

        test_args = ["screenshot.py", "--auth", "/admin/data-quality"]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()

        # Build the mock module tree that ``from playwright.sync_api import
        # sync_playwright`` will resolve to.
        mock_sync_api = MagicMock()
        mock_sync_api.sync_playwright = mock_sync_pw_fn
        mock_pw_pkg = MagicMock()

        with (
            patch("sys.argv", test_args),
            patch(
                "screenshot.fetch_credentials",
                return_value=("a@b.com", "pw"),
            ) as mock_fetch,
            patch("screenshot.perform_login") as mock_login,
            patch.dict(
                "sys.modules",
                {
                    "playwright": mock_pw_pkg,
                    "playwright.sync_api": mock_sync_api,
                },
            ),
        ):
            main()

        mock_fetch.assert_called_once()
        mock_login.assert_called_once_with(mock_page, "a@b.com", "pw")

    def test_no_auth_skips_login(self) -> None:
        """Verify that without --auth, no login is attempted."""
        from screenshot import main

        test_args = ["screenshot.py", "/rulings"]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()

        mock_sync_api = MagicMock()
        mock_sync_api.sync_playwright = mock_sync_pw_fn
        mock_pw_pkg = MagicMock()

        with (
            patch("sys.argv", test_args),
            patch("screenshot.fetch_credentials") as mock_fetch,
            patch("screenshot.perform_login") as mock_login,
            patch.dict(
                "sys.modules",
                {
                    "playwright": mock_pw_pkg,
                    "playwright.sync_api": mock_sync_api,
                },
            ),
        ):
            main()

        mock_fetch.assert_not_called()
        mock_login.assert_not_called()

        # Verify goto was called with the target URL directly
        first_goto = mock_page.goto.call_args_list[0]
        assert "rulings" in first_goto[0][0]

    def test_auth_navigates_to_target_after_login(self) -> None:
        """Verify that after login, the script navigates to the target URL."""
        from screenshot import main

        test_args = ["screenshot.py", "--auth", "/admin/data-quality"]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()

        mock_sync_api = MagicMock()
        mock_sync_api.sync_playwright = mock_sync_pw_fn
        mock_pw_pkg = MagicMock()

        with (
            patch("sys.argv", test_args),
            patch(
                "screenshot.fetch_credentials",
                return_value=("a@b.com", "pw"),
            ),
            patch("screenshot.perform_login"),
            patch.dict(
                "sys.modules",
                {
                    "playwright": mock_pw_pkg,
                    "playwright.sync_api": mock_sync_api,
                },
            ),
        ):
            main()

        # The target URL navigation happens after login
        target_goto = mock_page.goto.call_args_list[0]
        assert "admin/data-quality" in target_goto[0][0]
