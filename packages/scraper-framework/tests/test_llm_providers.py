"""Tests for the LLM provider adapter layer (llm_providers.py).

All tests mock the provider APIs — no real API calls are made (except the SDK
surface smoke test which instantiates a client with a fake key to verify the
API shape).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ingestion.llm_providers import (
    LLMResponse,
    _call_anthropic,
    _call_google,
    call_llm,
    create_client,
)

# ---------------------------------------------------------------------------
# Anthropic adapter tests
# ---------------------------------------------------------------------------


class TestCallAnthropic:
    """Tests for the Anthropic provider adapter."""

    def test_happy_path(self) -> None:
        """Successful Anthropic API call returns LLMResponse."""
        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_block = MagicMock()
        mock_block.text = '{"judge_name": "Test"}'
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage

        client = MagicMock()
        client.messages.create.return_value = mock_response

        result = _call_anthropic(
            system_prompt="You are a parser.",
            user_message="Extract fields.",
            model="claude-haiku-4-5-20251001",
            client=client,
        )

        assert result is not None
        assert isinstance(result, LLMResponse)
        assert result.text == '{"judge_name": "Test"}'
        assert result.input_tokens == 100
        assert result.output_tokens == 50

        call_kwargs = client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
        assert call_kwargs["temperature"] == 0
        assert call_kwargs["max_tokens"] == 4096

    def test_rate_limit_retries_once(self) -> None:
        """On rate limit, retries once with backoff then succeeds."""
        import anthropic

        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_block = MagicMock()
        mock_block.text = "ok"
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage

        rate_error = anthropic.RateLimitError(
            message="Rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )

        client = MagicMock()
        client.messages.create.side_effect = [rate_error, mock_response]

        with patch("ingestion.llm_providers.time.sleep") as mock_sleep:
            result = _call_anthropic(
                system_prompt="sys",
                user_message="user",
                model="test-model",
                client=client,
                max_retries=1,
            )

        assert result is not None
        assert result.text == "ok"
        mock_sleep.assert_called_once_with(1)
        assert client.messages.create.call_count == 2

    def test_rate_limit_exhausted_returns_none(self) -> None:
        """On double rate limit, returns None."""
        import anthropic

        rate_error = anthropic.RateLimitError(
            message="Rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )

        client = MagicMock()
        client.messages.create.side_effect = rate_error

        with patch("ingestion.llm_providers.time.sleep"):
            result = _call_anthropic(
                system_prompt="sys",
                user_message="user",
                model="test-model",
                client=client,
                max_retries=1,
            )

        assert result is None

    def test_api_error_returns_none(self) -> None:
        """Non-retryable API error returns None."""
        import anthropic

        client = MagicMock()
        client.messages.create.side_effect = anthropic.APIError(
            message="Server error",
            request=MagicMock(),
            body=None,
        )

        result = _call_anthropic(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
        )

        assert result is None

    def test_creates_client_from_env_when_none(self) -> None:
        """When no client is passed, creates one from ANTHROPIC_API_KEY."""
        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_block = MagicMock()
        mock_block.text = "response"
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage

        mock_client_instance = MagicMock()
        mock_client_instance.messages.create.return_value = mock_response

        with patch("anthropic.Anthropic", return_value=mock_client_instance):
            result = _call_anthropic(
                system_prompt="sys",
                user_message="user",
                model="test-model",
                client=None,
            )

        assert result is not None
        assert result.text == "response"


# ---------------------------------------------------------------------------
# Google adapter tests
# ---------------------------------------------------------------------------


class TestCallGoogle:
    """Tests for the Google GenAI provider adapter."""

    def _make_mock_response(
        self,
        text: str = '{"judge_name": "Test"}',
        input_tokens: int = 100,
        output_tokens: int = 50,
    ) -> MagicMock:
        usage = MagicMock()
        usage.prompt_token_count = input_tokens
        usage.candidates_token_count = output_tokens

        response = MagicMock()
        response.text = text
        response.usage_metadata = usage
        return response

    def test_happy_path(self) -> None:
        """Successful Google API call returns LLMResponse."""
        mock_response = self._make_mock_response()

        client = MagicMock()
        client.models.generate_content.return_value = mock_response

        result = _call_google(
            system_prompt="You are a parser.",
            user_message="Extract fields.",
            model="gemini-2.5-flash-lite",
            client=client,
        )

        assert result is not None
        assert isinstance(result, LLMResponse)
        assert result.text == '{"judge_name": "Test"}'
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    def test_api_error_returns_none(self) -> None:
        """API error returns None."""
        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("API failed")

        result = _call_google(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
        )

        assert result is None

    def test_resource_exhausted_retries(self) -> None:
        """ResourceExhausted error triggers retry."""
        mock_response = self._make_mock_response(text="ok")

        # Create a custom exception class to simulate ResourceExhausted
        class ResourceExhaustedError(Exception):
            pass

        client = MagicMock()
        client.models.generate_content.side_effect = [
            ResourceExhaustedError("quota exceeded"),
            mock_response,
        ]

        with patch("ingestion.llm_providers.time.sleep") as mock_sleep:
            result = _call_google(
                system_prompt="sys",
                user_message="user",
                model="test-model",
                client=client,
                max_retries=1,
            )

        assert result is not None
        assert result.text == "ok"
        mock_sleep.assert_called_once_with(1)

    def test_missing_api_key_returns_none(self) -> None:
        """When no client and no GOOGLE_API_KEY, returns None."""
        with patch.dict("os.environ", {}, clear=True):
            result = _call_google(
                system_prompt="sys",
                user_message="user",
                model="test-model",
                client=None,
            )

        assert result is None

    def test_none_usage_metadata(self) -> None:
        """Handles response with no usage metadata gracefully."""
        response = MagicMock()
        response.text = "result"
        response.usage_metadata = None

        client = MagicMock()
        client.models.generate_content.return_value = response

        result = _call_google(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
        )

        assert result is not None
        assert result.input_tokens == 0
        assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# call_llm dispatch tests
# ---------------------------------------------------------------------------


class TestCallLlm:
    """Tests for the top-level call_llm dispatcher."""

    def test_defaults_to_google(self) -> None:
        """With no provider specified, defaults to google."""
        with (
            patch("ingestion.llm_providers._call_google") as mock_google,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_google.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            result = call_llm(system_prompt="sys", user_message="user")

        assert result is not None
        mock_google.assert_called_once()
        # Verify default model
        call_args = mock_google.call_args
        assert call_args[0][2] == "gemini-2.5-flash-lite"

    def test_provider_from_env(self) -> None:
        """LLM_PROVIDER env var selects provider (anthropic overrides google default)."""
        with (
            patch("ingestion.llm_providers._call_anthropic") as mock_anthropic,
            patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}, clear=True),
        ):
            mock_anthropic.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            result = call_llm(system_prompt="sys", user_message="user")

        assert result is not None
        mock_anthropic.assert_called_once()

    def test_model_from_env(self) -> None:
        """LLM_MODEL env var overrides default model."""
        with (
            patch("ingestion.llm_providers._call_google") as mock_google,
            patch.dict("os.environ", {"LLM_MODEL": "gemini-2.0-flash"}, clear=True),
        ):
            mock_google.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            call_llm(system_prompt="sys", user_message="user")

        call_args = mock_google.call_args
        assert call_args[0][2] == "gemini-2.0-flash"

    def test_explicit_args_override_env(self) -> None:
        """Explicit provider/model args override env vars."""
        with (
            patch("ingestion.llm_providers._call_google") as mock_google,
            patch.dict(
                "os.environ",
                {"LLM_PROVIDER": "anthropic", "LLM_MODEL": "wrong"},
                clear=True,
            ),
        ):
            mock_google.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            call_llm(
                system_prompt="sys",
                user_message="user",
                provider="google",
                model="gemini-2.5-flash-lite",
            )

        mock_google.assert_called_once()
        call_args = mock_google.call_args
        assert call_args[0][2] == "gemini-2.5-flash-lite"

    def test_unknown_provider_returns_none(self) -> None:
        """Unknown provider name returns None."""
        result = call_llm(
            system_prompt="sys",
            user_message="user",
            provider="openai",
        )
        assert result is None

    def test_client_passed_through(self) -> None:
        """Pre-created client is forwarded to the provider adapter."""
        mock_client = MagicMock()
        with patch("ingestion.llm_providers._call_anthropic") as mock_anthropic:
            mock_anthropic.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            call_llm(
                system_prompt="sys",
                user_message="user",
                provider="anthropic",
                client=mock_client,
            )

        call_kwargs = mock_anthropic.call_args.kwargs
        assert call_kwargs["client"] is mock_client


# ---------------------------------------------------------------------------
# create_client tests
# ---------------------------------------------------------------------------


class TestCreateClient:
    """Tests for the create_client factory."""

    def test_anthropic_with_key(self) -> None:
        """Creates Anthropic client when API key is available."""
        mock_instance = MagicMock()
        with (
            patch("anthropic.Anthropic", return_value=mock_instance),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            client = create_client(provider="anthropic")

        assert client is mock_instance

    def test_anthropic_without_key(self) -> None:
        """Returns None when ANTHROPIC_API_KEY is missing."""
        with patch.dict("os.environ", {}, clear=True):
            client = create_client(provider="anthropic")

        assert client is None

    def test_google_with_key(self) -> None:
        """Creates Google client when API key is available."""
        mock_instance = MagicMock()
        with (
            patch("google.genai.Client", return_value=mock_instance),
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}),
        ):
            client = create_client(provider="google")

        assert client is mock_instance

    def test_google_without_key(self) -> None:
        """Returns None when GOOGLE_API_KEY is missing."""
        with patch.dict("os.environ", {}, clear=True):
            client = create_client(provider="google")

        assert client is None

    def test_defaults_to_google(self) -> None:
        """With no provider arg, defaults to google."""
        mock_instance = MagicMock()
        with (
            patch("google.genai.Client", return_value=mock_instance),
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}, clear=True),
        ):
            client = create_client()

        assert client is mock_instance

    def test_provider_from_env(self) -> None:
        """LLM_PROVIDER env var selects provider (anthropic overrides google default)."""
        mock_instance = MagicMock()
        with (
            patch("anthropic.Anthropic", return_value=mock_instance),
            patch.dict("os.environ", {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"}),
        ):
            client = create_client()

        assert client is mock_instance

    def test_unknown_provider_returns_none(self) -> None:
        """Unknown provider returns None."""
        client = create_client(provider="openai")
        assert client is None


# ---------------------------------------------------------------------------
# SDK surface smoke tests (real SDK, no API calls)
# ---------------------------------------------------------------------------


class TestGoogleSdkSurface:
    """Verify the installed google-genai SDK exposes the expected API surface.

    These tests instantiate a real ``genai.Client`` with a fake API key —
    no network calls are made.  They exist to catch SDK version mismatches
    or namespace conflicts (e.g. ``google-generativeai`` shadowing
    ``google-genai``) before deployment.
    """

    def test_client_models_has_generate_content(self) -> None:
        """client.models must have a generate_content method."""
        from google import genai

        client = genai.Client(api_key="fake-key-for-test")
        assert hasattr(client.models, "generate_content"), (
            f"client.models ({type(client.models).__qualname__}) is missing "
            "generate_content — check installed google-genai version"
        )

    def test_client_models_generate_content_is_callable(self) -> None:
        """generate_content must be callable (not just an attribute)."""
        from google import genai

        client = genai.Client(api_key="fake-key-for-test")
        assert callable(client.models.generate_content)

    def test_generate_content_config_type_exists(self) -> None:
        """GenerateContentConfig must be importable from google.genai.types."""
        from google.genai import types

        assert hasattr(types, "GenerateContentConfig")


class TestGoogleDefensiveCheck:
    """Test the defensive SDK compatibility check in _call_google."""

    def test_incompatible_client_returns_none(self) -> None:
        """If client.models lacks generate_content, returns None immediately."""
        # Simulate an incompatible client where models has no generate_content
        client = MagicMock()
        del client.models.generate_content  # remove the auto-created attr

        result = _call_google(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
        )

        assert result is None
