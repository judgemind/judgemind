"""Tests for the LLM provider adapter layer (llm_providers.py).

All tests mock the provider APIs — no real API calls are made (except the SDK
surface smoke test which instantiates a client with a fake key to verify the
API shape).
"""

from __future__ import annotations

import importlib.metadata
from unittest.mock import MagicMock, patch

import anthropic
import pytest
from google import genai

from ingestion.llm_providers import (
    LLMResponse,
    _call_anthropic,
    _call_google,
    _try_create,
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
# _try_create tests
# ---------------------------------------------------------------------------


class TestTryCreate:
    """Tests for the _try_create() helper."""

    def test_anthropic_with_key(self) -> None:
        """Creates Anthropic client when API key is available."""
        mock_instance = MagicMock()
        with (
            patch("anthropic.Anthropic", return_value=mock_instance),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        ):
            client = _try_create("anthropic")
        assert client is mock_instance

    def test_anthropic_without_key(self) -> None:
        """Returns None when ANTHROPIC_API_KEY is missing."""
        with patch.dict("os.environ", {}, clear=True):
            assert _try_create("anthropic") is None

    def test_google_with_key(self) -> None:
        """Creates Google client when API key is available."""
        mock_instance = MagicMock()
        with (
            patch("google.genai.Client", return_value=mock_instance),
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}),
        ):
            client = _try_create("google")
        assert client is mock_instance

    def test_google_without_key(self) -> None:
        """Returns None when GOOGLE_API_KEY is missing."""
        with patch.dict("os.environ", {}, clear=True):
            assert _try_create("google") is None

    def test_unknown_provider(self) -> None:
        """Returns None for an unsupported provider."""
        assert _try_create("openai") is None


# ---------------------------------------------------------------------------
# create_client fallback tests
# ---------------------------------------------------------------------------


class TestCreateClientFallback:
    """Tests for the provider fallback logic in create_client()."""

    def test_fallback_google_to_anthropic(self) -> None:
        """When configured for google but only ANTHROPIC_API_KEY is set, falls back."""
        mock_instance = MagicMock()
        with (
            patch("anthropic.Anthropic", return_value=mock_instance),
            patch.dict(
                "os.environ",
                {"LLM_PROVIDER": "google", "ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ),
        ):
            client = create_client()
        assert client is mock_instance

    def test_fallback_anthropic_to_google(self) -> None:
        """When configured for anthropic but only GOOGLE_API_KEY is set, falls back."""
        mock_instance = MagicMock()
        with (
            patch("google.genai.Client", return_value=mock_instance),
            patch.dict(
                "os.environ",
                {"LLM_PROVIDER": "anthropic", "GOOGLE_API_KEY": "test-key"},
                clear=True,
            ),
        ):
            client = create_client()
        assert client is mock_instance

    def test_no_fallback_when_primary_works(self) -> None:
        """When primary provider's key is available, no fallback occurs."""
        mock_instance = MagicMock()
        with (
            patch("google.genai.Client", return_value=mock_instance),
            patch.dict(
                "os.environ",
                {"GOOGLE_API_KEY": "test-key"},
                clear=True,
            ),
        ):
            client = create_client(provider="google")
        assert client is mock_instance

    def test_no_keys_returns_none(self) -> None:
        """When no provider's key is available, returns None."""
        with patch.dict("os.environ", {}, clear=True):
            assert create_client() is None

    def test_explicit_provider_with_fallback(self) -> None:
        """Explicit provider arg still falls back when its key is missing."""
        mock_instance = MagicMock()
        with (
            patch("google.genai.Client", return_value=mock_instance),
            patch.dict(
                "os.environ",
                {"GOOGLE_API_KEY": "test-key"},
                clear=True,
            ),
        ):
            client = create_client(provider="anthropic")
        assert client is mock_instance

    def test_fallback_logs_warning(self) -> None:
        """Fallback logs a warning with configured and actual provider."""
        mock_instance = MagicMock()
        with (
            patch("anthropic.Anthropic", return_value=mock_instance),
            patch("ingestion.llm_providers.logger") as mock_logger,
            patch.dict(
                "os.environ",
                {"LLM_PROVIDER": "google", "ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ),
        ):
            create_client()
        mock_logger.warning.assert_called_once_with(
            "llm_providers.fallback",
            configured="google",
            using="anthropic",
        )

    def test_unknown_provider_still_falls_back(self) -> None:
        """An unknown primary provider triggers fallback to known providers."""
        mock_instance = MagicMock()
        with (
            patch("anthropic.Anthropic", return_value=mock_instance),
            patch.dict(
                "os.environ",
                {"ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ),
        ):
            client = create_client(provider="openai")
        assert client is mock_instance


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


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------


class TestAnthropicTimeout:
    """Tests for timeout behavior in the Anthropic adapter."""

    def test_timeout_returns_none(self) -> None:
        """APITimeoutError returns None immediately."""
        import anthropic

        client = MagicMock()
        client.messages.create.side_effect = anthropic.APITimeoutError(
            request=MagicMock(),
        )

        result = _call_anthropic(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
            timeout=10.0,
        )

        assert result is None

    def test_timeout_param_passed_to_sdk(self) -> None:
        """The timeout parameter is forwarded to the SDK create() call."""
        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_block = MagicMock()
        mock_block.text = "ok"
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage

        client = MagicMock()
        client.messages.create.return_value = mock_response

        _call_anthropic(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
            timeout=30.0,
        )

        call_kwargs = client.messages.create.call_args.kwargs
        assert call_kwargs["timeout"] == 30.0

    def test_no_timeout_param_when_none(self) -> None:
        """When timeout is None, no timeout kwarg is passed to the SDK."""
        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_block = MagicMock()
        mock_block.text = "ok"
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage = mock_usage

        client = MagicMock()
        client.messages.create.return_value = mock_response

        _call_anthropic(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
            timeout=None,
        )

        call_kwargs = client.messages.create.call_args.kwargs
        assert "timeout" not in call_kwargs


class TestGoogleTimeout:
    """Tests for timeout behavior in the Google adapter."""

    def test_deadline_exceeded_returns_none(self) -> None:
        """DeadlineExceeded error returns None immediately (no retries)."""

        class DeadlineExceededError(Exception):
            pass

        client = MagicMock()
        client.models.generate_content.side_effect = DeadlineExceededError("deadline exceeded")

        result = _call_google(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
            timeout=10.0,
            max_retries=3,
        )

        assert result is None
        # Should NOT retry on timeout — only one call
        assert client.models.generate_content.call_count == 1

    def test_timeout_error_returns_none(self) -> None:
        """TimeoutError returns None immediately."""

        class HTTPTimeoutError(TimeoutError):
            pass

        client = MagicMock()
        client.models.generate_content.side_effect = HTTPTimeoutError("timed out")

        result = _call_google(
            system_prompt="sys",
            user_message="user",
            model="test-model",
            client=client,
            timeout=10.0,
        )

        assert result is None


class TestCallLlmTimeout:
    """Tests for timeout passthrough in the call_llm dispatcher."""

    def test_timeout_forwarded_to_anthropic(self) -> None:
        """Timeout parameter is forwarded to the Anthropic adapter."""
        with (
            patch("ingestion.llm_providers._call_anthropic") as mock_anthropic,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_anthropic.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            call_llm(
                system_prompt="sys",
                user_message="user",
                provider="anthropic",
                timeout=45.0,
            )

        call_kwargs = mock_anthropic.call_args.kwargs
        assert call_kwargs["timeout"] == 45.0

    def test_timeout_forwarded_to_google(self) -> None:
        """Timeout parameter is forwarded to the Google adapter."""
        with (
            patch("ingestion.llm_providers._call_google") as mock_google,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_google.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            call_llm(
                system_prompt="sys",
                user_message="user",
                provider="google",
                timeout=45.0,
            )

        call_kwargs = mock_google.call_args.kwargs
        assert call_kwargs["timeout"] == 45.0

    def test_timeout_defaults_to_none(self) -> None:
        """When timeout is not specified, None is forwarded."""
        with (
            patch("ingestion.llm_providers._call_google") as mock_google,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_google.return_value = LLMResponse(text="ok", input_tokens=1, output_tokens=1)
            call_llm(system_prompt="sys", user_message="user")

        call_kwargs = mock_google.call_args.kwargs
        assert call_kwargs["timeout"] is None


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


# ---------------------------------------------------------------------------
# SDK namespace conflict detection (CI integration tests)
# ---------------------------------------------------------------------------


class TestSdkNamespaceConflict:
    """Detect conflicting Google GenAI SDK packages.

    The ``google-generativeai`` package installs into the same ``google``
    namespace as ``google-genai`` and shadows its modules.  If both are
    installed, our adapter silently breaks at runtime.  These tests catch
    the conflict in CI before it reaches production.
    """

    def test_google_generativeai_not_installed(self) -> None:
        """google-generativeai must NOT be installed alongside google-genai.

        The two packages share the ``google`` namespace and conflict at
        runtime.  Our adapter requires ``google-genai`` only.
        """
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("google-generativeai")

    def test_google_genai_is_installed(self) -> None:
        """google-genai must be installed (it's our required SDK)."""
        version = importlib.metadata.version("google-genai")
        assert version, "google-genai is installed but has no version string"


# ---------------------------------------------------------------------------
# create_client type verification (real SDK, fake keys, no API calls)
# ---------------------------------------------------------------------------


class TestCreateClientTypes:
    """Verify create_client() returns the correct SDK client types.

    These tests use real SDK classes with fake API keys — no network calls
    are made.  They ensure the adapter layer returns objects whose types
    match what our code expects, catching version mismatches or incorrect
    imports.
    """

    def test_google_client_type(self) -> None:
        """create_client('google') returns a google.genai.Client instance."""
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "fake-key-for-test"}):
            client = create_client(provider="google")

        assert client is not None
        assert isinstance(client, genai.Client)

    def test_google_client_has_models_attr(self) -> None:
        """Google client must have a ``models`` attribute with generate_content."""
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "fake-key-for-test"}):
            client = create_client(provider="google")

        assert client is not None
        assert hasattr(client, "models")
        assert hasattr(client.models, "generate_content")
        assert callable(client.models.generate_content)

    def test_anthropic_client_type(self) -> None:
        """create_client('anthropic') returns an anthropic.Anthropic instance."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-test"}):
            client = create_client(provider="anthropic")

        assert client is not None
        assert isinstance(client, anthropic.Anthropic)

    def test_anthropic_client_has_messages_attr(self) -> None:
        """Anthropic client must have a ``messages`` attribute with create."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key-for-test"}):
            client = create_client(provider="anthropic")

        assert client is not None
        assert hasattr(client, "messages")
        assert hasattr(client.messages, "create")
        assert callable(client.messages.create)

    def test_missing_key_returns_none(self) -> None:
        """create_client returns None when API key is missing."""
        with patch.dict("os.environ", {}, clear=True):
            assert create_client(provider="google") is None
            assert create_client(provider="anthropic") is None

    def test_unknown_provider_returns_none(self) -> None:
        """create_client returns None for an unsupported provider name."""
        assert create_client(provider="openai") is None
