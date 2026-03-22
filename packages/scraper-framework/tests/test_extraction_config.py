"""Tests for ingestion.extraction_config — per-county extraction method routing."""

from __future__ import annotations

import pytest

from ingestion.extraction_config import (
    ExtractionMethod,
    get_extraction_method,
    reset_extraction_methods,
    set_extraction_method,
)


@pytest.fixture(autouse=True)
def _reset_config() -> None:  # noqa: ANN202
    """Reset the extraction method table after each test."""
    yield
    reset_extraction_methods()


class TestGetExtractionMethod:
    """Tests for get_extraction_method."""

    def test_default_is_regex(self) -> None:
        """Counties without explicit config default to regex."""
        assert get_extraction_method("CA", "Los Angeles") == ExtractionMethod.REGEX

    def test_orange_county_is_llm(self) -> None:
        """Orange County is configured for LLM extraction."""
        assert get_extraction_method("CA", "Orange") == ExtractionMethod.LLM

    def test_unknown_state_defaults_to_regex(self) -> None:
        """Unknown state/county combos default to regex."""
        assert get_extraction_method("TX", "Harris") == ExtractionMethod.REGEX

    def test_set_extraction_method(self) -> None:
        """set_extraction_method updates the config for a county."""
        assert get_extraction_method("CA", "Riverside") == ExtractionMethod.REGEX
        set_extraction_method("CA", "Riverside", ExtractionMethod.LLM)
        assert get_extraction_method("CA", "Riverside") == ExtractionMethod.LLM

    def test_reset_extraction_methods(self) -> None:
        """reset_extraction_methods restores defaults."""
        set_extraction_method("CA", "Riverside", ExtractionMethod.LLM)
        reset_extraction_methods()
        assert get_extraction_method("CA", "Riverside") == ExtractionMethod.REGEX
        # OC should still be LLM after reset
        assert get_extraction_method("CA", "Orange") == ExtractionMethod.LLM

    def test_env_override_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides the hardcoded config to llm."""
        monkeypatch.setenv("EXTRACTION_METHOD_CA_LOS_ANGELES", "llm")
        assert get_extraction_method("CA", "Los Angeles") == ExtractionMethod.LLM

    def test_env_override_regex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable can override LLM back to regex."""
        monkeypatch.setenv("EXTRACTION_METHOD_CA_ORANGE", "regex")
        assert get_extraction_method("CA", "Orange") == ExtractionMethod.REGEX

    def test_env_override_invalid_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid environment variable value is ignored."""
        monkeypatch.setenv("EXTRACTION_METHOD_CA_ORANGE", "invalid_method")
        # Should fall through to the hardcoded LLM config
        assert get_extraction_method("CA", "Orange") == ExtractionMethod.LLM

    def test_env_override_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable values are case-insensitive."""
        monkeypatch.setenv("EXTRACTION_METHOD_CA_RIVERSIDE", "LLM")
        assert get_extraction_method("CA", "Riverside") == ExtractionMethod.LLM

    def test_county_with_spaces_in_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """County names with spaces have spaces replaced with underscores in env key."""
        monkeypatch.setenv("EXTRACTION_METHOD_CA_SAN_BERNARDINO", "llm")
        assert get_extraction_method("CA", "San Bernardino") == ExtractionMethod.LLM


class TestExtractionMethodEnum:
    """Tests for the ExtractionMethod enum."""

    def test_values(self) -> None:
        assert ExtractionMethod.REGEX == "regex"
        assert ExtractionMethod.LLM == "llm"

    def test_from_string(self) -> None:
        assert ExtractionMethod("regex") == ExtractionMethod.REGEX
        assert ExtractionMethod("llm") == ExtractionMethod.LLM

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            ExtractionMethod("invalid")
