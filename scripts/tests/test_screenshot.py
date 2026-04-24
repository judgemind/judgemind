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


class TestClickFlag:
    """Tests for the --click and --click-wait flags.

    Mirrors the patching pattern used by TestMainAuthIntegration so the lazy
    ``from playwright.sync_api import sync_playwright`` inside ``main()``
    works without playwright installed.
    """

    def _patch_modules(self, mock_sync_pw_fn: MagicMock) -> dict[str, MagicMock]:
        mock_sync_api = MagicMock()
        mock_sync_api.sync_playwright = mock_sync_pw_fn
        mock_pw_pkg = MagicMock()
        return {"playwright": mock_pw_pkg, "playwright.sync_api": mock_sync_api}

    def test_click_invokes_query_selector_then_click(self) -> None:
        """--click <sel> calls query_selector(sel) and then .click() on the result."""
        from screenshot import main

        test_args = [
            "screenshot.py",
            "/rulings",
            "--click",
            '[data-testid="queue-ready-count"]',
        ]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()

        mock_click_target = MagicMock()
        mock_page.query_selector.return_value = mock_click_target

        with (
            patch("sys.argv", test_args),
            patch.dict("sys.modules", self._patch_modules(mock_sync_pw_fn)),
        ):
            main()

        mock_page.query_selector.assert_called_once_with(
            '[data-testid="queue-ready-count"]'
        )
        mock_click_target.click.assert_called_once_with()

    def test_click_default_wait_is_500_ms(self) -> None:
        """Default --click-wait is 500ms; called via page.wait_for_timeout after click."""
        from screenshot import main

        test_args = ["screenshot.py", "/rulings", "--click", ".btn"]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()
        mock_page.query_selector.return_value = MagicMock()

        with (
            patch("sys.argv", test_args),
            patch.dict("sys.modules", self._patch_modules(mock_sync_pw_fn)),
        ):
            main()

        # The default --wait is 3000ms (the existing post-load wait), then 500ms
        # after the click — both go through page.wait_for_timeout.
        wait_calls = [c.args[0] for c in mock_page.wait_for_timeout.call_args_list]
        assert 500 in wait_calls

    def test_click_wait_zero_skips_post_click_wait(self) -> None:
        """--click-wait 0 must not invoke wait_for_timeout(0) after the click."""
        from screenshot import main

        test_args = [
            "screenshot.py",
            "/rulings",
            "--click",
            ".btn",
            "--click-wait",
            "0",
            "--wait",
            "0",
        ]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()
        mock_page.query_selector.return_value = MagicMock()

        with (
            patch("sys.argv", test_args),
            patch.dict("sys.modules", self._patch_modules(mock_sync_pw_fn)),
        ):
            main()

        # With both --wait 0 and --click-wait 0, no timeout calls fire at all.
        mock_page.wait_for_timeout.assert_not_called()

    def test_click_wait_custom_value(self) -> None:
        """--click-wait <N> passes N to page.wait_for_timeout."""
        from screenshot import main

        test_args = [
            "screenshot.py",
            "/rulings",
            "--click",
            ".btn",
            "--click-wait",
            "1500",
            "--wait",
            "0",
        ]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()
        mock_page.query_selector.return_value = MagicMock()

        with (
            patch("sys.argv", test_args),
            patch.dict("sys.modules", self._patch_modules(mock_sync_pw_fn)),
        ):
            main()

        wait_calls = [c.args[0] for c in mock_page.wait_for_timeout.call_args_list]
        assert wait_calls == [1500]

    def test_click_missing_selector_exits_with_clear_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If the click target is not found, exit non-zero with a one-line error."""
        from screenshot import main

        test_args = [
            "screenshot.py",
            "/rulings",
            "--click",
            '[data-testid="does-not-exist"]',
        ]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()
        mock_page.query_selector.return_value = None

        with (
            patch("sys.argv", test_args),
            patch.dict("sys.modules", self._patch_modules(mock_sync_pw_fn)),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert 'Click target not found: [data-testid="does-not-exist"]' in captured.err

    def test_no_click_flag_skips_click_entirely(self) -> None:
        """Without --click, neither query_selector nor an extra wait fires."""
        from screenshot import main

        test_args = ["screenshot.py", "/rulings", "--wait", "0"]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()

        with (
            patch("sys.argv", test_args),
            patch.dict("sys.modules", self._patch_modules(mock_sync_pw_fn)),
        ):
            main()

        # No click means no query_selector for a click target. (--selector
        # would also call query_selector, but we did not pass it here.)
        mock_page.query_selector.assert_not_called()
        # No --click and --wait 0 means no wait_for_timeout calls at all.
        mock_page.wait_for_timeout.assert_not_called()

    def test_click_runs_after_wait_and_before_screenshot(self) -> None:
        """Order must be: goto → wait_for_timeout (page wait) → click → wait_for_timeout (click wait) → screenshot."""
        from screenshot import main

        test_args = ["screenshot.py", "/rulings", "--click", ".btn"]
        mock_sync_pw_fn, mock_page, _mock_browser = _make_mock_playwright()

        # Build a single mock that records every relevant call so we can
        # inspect ordering.
        mock_click_target = MagicMock()
        mock_page.query_selector.return_value = mock_click_target

        # Use a parent MagicMock to record order across attributes.
        ordered = MagicMock()
        ordered.attach_mock(mock_page.goto, "goto")
        ordered.attach_mock(mock_page.wait_for_timeout, "wait_for_timeout")
        ordered.attach_mock(mock_page.query_selector, "query_selector")
        ordered.attach_mock(mock_click_target.click, "click")
        ordered.attach_mock(mock_page.screenshot, "screenshot")

        with (
            patch("sys.argv", test_args),
            patch.dict("sys.modules", self._patch_modules(mock_sync_pw_fn)),
        ):
            main()

        names = [c[0] for c in ordered.mock_calls]
        # Expected sequence (using default --wait 3000 and --click-wait 500):
        # goto, wait_for_timeout(3000), query_selector(.btn), click(),
        # wait_for_timeout(500), screenshot(...)
        assert names == [
            "goto",
            "wait_for_timeout",
            "query_selector",
            "click",
            "wait_for_timeout",
            "screenshot",
        ]
