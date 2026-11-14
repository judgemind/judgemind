"""Required extraction mode builds the extractors this deployment uses (#4957).

#4953 made ``required`` the default extraction mode, and its startup check
always built the framework extractor with Anthropic.  Production sets
``LLM_PROVIDER=google`` and wires only ``GOOGLE_API_KEY``.  The framework
extractor now runs on ``LLM_PROVIDER``, like every other LLM client.

The production env-shape regression tests live in
``test_extraction_mode_4953.py`` (``-k production_env_shape``) and use only
the API that existed before this fix, so they fail on 35b1fe6.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from framework import extraction_config
from framework.extraction_config import CountyExtractionConfig, configured_providers
from ingestion.worker import (
    EXTRACTION_MODE_ENV,
    ExtractorUnavailableError,
    IngestionWorker,
    check_extraction_ready,
    framework_llm_provider,
)


def _make_worker() -> IngestionWorker:
    return IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production's ingestion-worker env shape: ``LLM_PROVIDER`` is google
    and only the Google key is wired."""
    monkeypatch.setenv(EXTRACTION_MODE_ENV, "required")
    monkeypatch.setenv("LLM_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No S3 LLM cache: the test must not reach the network.
    monkeypatch.setenv("JUDGEMIND_ARCHIVE_BUCKET", "")


def test_production_env_shape_builds_only_google_extractors(production_env: None) -> None:
    with patch("ingestion.worker.LlmExtractor") as cls:
        _make_worker()
        check_extraction_ready()
    assert [c.kwargs["provider"] for c in cls.call_args_list] == ["google"] * 4


def test_an_anthropic_deployment_refuses_without_the_anthropic_key(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LLM_PROVIDER=anthropic`` with no Anthropic key still refuses to start
    (#4953's guarantee), from the real ``LlmExtractor``."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(
        ExtractorUnavailableError,
        match="extractor unavailable: framework: ValueError: ANTHROPIC_API_KEY is not set",
    ):
        _make_worker()
    with pytest.raises(ExtractorUnavailableError, match="framework: .*ANTHROPIC_API_KEY"):
        check_extraction_ready()


def test_required_mode_refuses_without_the_google_key(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY")
    with pytest.raises(
        ExtractorUnavailableError, match="framework: ValueError.*multimodal: ValueError"
    ):
        _make_worker()


@pytest.mark.parametrize(
    ("llm_env", "arg", "expected"),
    [
        (None, None, "google"),
        ("", None, "google"),
        ("anthropic", None, "anthropic"),
        (" Google ", None, "google"),
        ("google", "anthropic", "anthropic"),
    ],
)
def test_framework_llm_provider_resolution(
    monkeypatch: pytest.MonkeyPatch, llm_env: str | None, arg: str | None, expected: str
) -> None:
    if llm_env is None:
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("LLM_PROVIDER", llm_env)
    assert framework_llm_provider(arg) == expected


def test_framework_llm_provider_rejects_an_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not silently pick the Anthropic client."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(ValueError, match="not a known LLM provider"):
        framework_llm_provider()


def test_configured_providers_are_the_registry_providers() -> None:
    assert configured_providers() == frozenset({"google"})


def test_required_mode_checks_a_county_provider_the_others_do_not_cover(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A county configured for a provider whose key is missing refuses at
    startup, not once per event."""
    monkeypatch.setitem(
        extraction_config._COUNTY_CONFIGS,
        ("CA", "TEST"),
        CountyExtractionConfig(provider="anthropic"),
    )
    with pytest.raises(ExtractorUnavailableError, match="county \\(anthropic\\): ValueError"):
        _make_worker()
    with pytest.raises(ExtractorUnavailableError, match="county \\(anthropic\\): ValueError"):
        check_extraction_ready()
