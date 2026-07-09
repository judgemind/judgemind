"""Integration tests: SD/SF scrapers run proxy-auth diagnosis on all-retries
failure when a proxy is configured (#4638).

These assert the wiring — that ``_solve_cloudflare`` / ``_acquire_session``
invoke ``diagnose_and_log_proxy_auth`` with the proxy URL after exhausting
retries — without standing up a full playwright stack.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import courts.ca.sd_tentatives as sd_mod
import courts.ca.sf_civil_tentatives as sf_mod
from courts.ca.sd_tentatives import SDTentativeRulingsScraper
from courts.ca.sf_civil_tentatives import SFCivilTentativeRulingsScraper
from framework.models import ScraperConfig

pytestmark = pytest.mark.regression

PROXY_URL = "http://user:pass@proxy.example:33335"


def _sd_config() -> ScraperConfig:
    return ScraperConfig(
        scraper_id="ca-sd-tentatives-test",
        state="CA",
        county="San Diego",
        court="Superior Court",
        target_urls=["https://odyroa.sdcourt.ca.gov"],
        request_delay_seconds=0.0,
    )


def _sf_config() -> ScraperConfig:
    return ScraperConfig(
        scraper_id="ca-sf-civil-tentatives-test",
        state="CA",
        county="San Francisco",
        court="Superior Court",
        target_urls=["https://webapps.sftc.org"],
        request_delay_seconds=0.0,
    )


class TestSDProxyAuthDiagnosisWiring:
    def test_diagnosis_invoked_on_all_retries_failure_with_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scraper = SDTentativeRulingsScraper(_sd_config(), case_numbers=["X"], proxy_url=PROXY_URL)
        assert scraper._proxy_url == PROXY_URL

        diag = MagicMock()
        monkeypatch.setattr(sd_mod, "diagnose_and_log_proxy_auth", diag)
        monkeypatch.setattr(sd_mod, "CF_MAX_RETRIES", 1)

        page = AsyncMock()
        page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_TIMED_OUT"))
        page.context = AsyncMock()

        result = asyncio.run(scraper._solve_cloudflare(page))

        assert result is False
        diag.assert_called_once()
        # Called with (self._log, self._proxy_url).
        args = diag.call_args.args
        assert args[1] == PROXY_URL

    def test_diagnosis_not_invoked_without_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scraper = SDTentativeRulingsScraper(_sd_config(), case_numbers=["X"])
        # Ensure no ambient proxy env leaks in.
        scraper._proxy_url = None

        diag = MagicMock()
        monkeypatch.setattr(sd_mod, "diagnose_and_log_proxy_auth", diag)
        monkeypatch.setattr(sd_mod, "CF_MAX_RETRIES", 1)

        page = AsyncMock()
        page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_TIMED_OUT"))
        page.context = AsyncMock()

        result = asyncio.run(scraper._solve_cloudflare(page))

        assert result is False
        diag.assert_not_called()


class TestSFProxyAuthDiagnosisWiring:
    def test_diagnosis_invoked_on_all_retries_failure_with_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        scraper = SFCivilTentativeRulingsScraper(config=_sf_config(), proxy_url=PROXY_URL)
        assert scraper._proxy_url == PROXY_URL

        diag = MagicMock()
        monkeypatch.setattr(sf_mod, "diagnose_and_log_proxy_auth", diag)
        monkeypatch.setattr(sf_mod, "SESSION_MAX_RETRIES", 1)

        async def _fail_try_acquire(_pw: Any) -> str | None:
            return None

        scraper._try_acquire_session = _fail_try_acquire  # type: ignore[assignment]

        result = asyncio.run(scraper._acquire_session())

        assert result is None
        diag.assert_called_once()
        args = diag.call_args.args
        assert args[1] == PROXY_URL

    def test_diagnosis_not_invoked_without_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
        monkeypatch.delenv("SF_PROXY_URL", raising=False)
        monkeypatch.delenv("SD_PROXY_URL", raising=False)
        scraper = SFCivilTentativeRulingsScraper(config=_sf_config())
        scraper._proxy_url = None

        diag = MagicMock()
        monkeypatch.setattr(sf_mod, "diagnose_and_log_proxy_auth", diag)
        monkeypatch.setattr(sf_mod, "SESSION_MAX_RETRIES", 1)

        async def _fail_try_acquire(_pw: Any) -> str | None:
            return None

        scraper._try_acquire_session = _fail_try_acquire  # type: ignore[assignment]

        result = asyncio.run(scraper._acquire_session())

        assert result is None
        diag.assert_not_called()
