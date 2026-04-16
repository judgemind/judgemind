"""Tests for framework.browser — shared Playwright stealth utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.browser import apply_stealth


class TestApplyStealth:
    """Tests for the shared apply_stealth utility."""

    @pytest.mark.asyncio
    async def test_applies_stealth_when_library_available(self) -> None:
        """Stealth().apply_stealth_async should be called when installed."""
        page = AsyncMock()
        mock_stealth_instance = MagicMock()
        mock_stealth_instance.apply_stealth_async = AsyncMock()
        mock_stealth_cls = MagicMock(return_value=mock_stealth_instance)

        with patch("playwright_stealth.Stealth", mock_stealth_cls):
            await apply_stealth(page)

        mock_stealth_cls.assert_called_once()
        mock_stealth_instance.apply_stealth_async.assert_awaited_once_with(page)

    @pytest.mark.asyncio
    async def test_fallback_when_stealth_not_installed(self) -> None:
        """Falls back to minimal webdriver override when stealth unavailable."""
        page = AsyncMock()
        with patch.dict("sys.modules", {"playwright_stealth": None}):
            await apply_stealth(page)
        page.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fallback_sets_webdriver_undefined(self) -> None:
        """The fallback evaluate call should remove the webdriver flag."""
        page = AsyncMock()
        with patch.dict("sys.modules", {"playwright_stealth": None}):
            await apply_stealth(page)
        call_args = page.evaluate.call_args[0][0]
        assert "webdriver" in call_args
        assert "undefined" in call_args

    @pytest.mark.asyncio
    async def test_does_not_raise_with_mock_page(self) -> None:
        """apply_stealth should handle a mock page without raising."""
        page = AsyncMock()
        # playwright-stealth is installed in the dev environment
        await apply_stealth(page)
