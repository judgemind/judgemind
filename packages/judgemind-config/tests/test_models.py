"""Tests for the centralized model constants."""

from __future__ import annotations

from unittest.mock import patch


class TestDefaultHaikuModel:
    """Tests for DEFAULT_HAIKU_MODEL constant."""

    def test_default_value_is_set(self) -> None:
        """The default Haiku model ID is a non-empty string."""
        from judgemind_config.models import DEFAULT_HAIKU_MODEL

        assert isinstance(DEFAULT_HAIKU_MODEL, str)
        assert len(DEFAULT_HAIKU_MODEL) > 0

    def test_default_value_contains_haiku(self) -> None:
        """The default model ID contains 'haiku' to guard against accidental misconfiguration."""
        from judgemind_config.models import DEFAULT_HAIKU_MODEL

        assert "haiku" in DEFAULT_HAIKU_MODEL.lower()

    def test_env_var_override(self) -> None:
        """Setting HAIKU_MODEL env var overrides the default."""
        with patch.dict("os.environ", {"HAIKU_MODEL": "claude-haiku-test-model"}):
            # Re-import to pick up the env var
            import importlib

            import judgemind_config.models

            importlib.reload(judgemind_config.models)
            assert judgemind_config.models.DEFAULT_HAIKU_MODEL == "claude-haiku-test-model"

            # Restore the original
            importlib.reload(judgemind_config.models)

    def test_exported_from_package(self) -> None:
        """DEFAULT_HAIKU_MODEL is accessible from the top-level package."""
        from judgemind_config import DEFAULT_HAIKU_MODEL

        assert isinstance(DEFAULT_HAIKU_MODEL, str)
        assert "haiku" in DEFAULT_HAIKU_MODEL.lower()
