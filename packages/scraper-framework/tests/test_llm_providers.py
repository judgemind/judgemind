"""Tests for the LLM provider adapter layer (llm_providers.py).

All tests mock the provider APIs — no real API calls are made (except the SDK
surface smoke test which instantiates a client with a fake key to verify the
API shape).
"""

from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest
import respx
import structlog
from google import genai
from google.genai import errors as genai_errors
from judgemind_config import DEFAULT_HAIKU_MODEL

from ingestion.llm_providers import (
    LLMResponse,
    _call_anthropic,
    _call_google,
    _call_google_with_images,
    _try_create,
    call_llm,
    call_llm_with_images,
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
            model=DEFAULT_HAIKU_MODEL,
            client=client,
        )

        assert result is not None
        assert isinstance(result, LLMResponse)
        assert result.text == '{"judge_name": "Test"}'
        assert result.input_tokens == 100
        assert result.output_tokens == 50

        call_kwargs = client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == DEFAULT_HAIKU_MODEL
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
# call_llm and call_llm_with_images default max_retries tests
# ---------------------------------------------------------------------------


class TestDefaultMaxRetries:
    """Verify that public API functions default to max_retries=2."""

    def test_call_llm_default_max_retries(self) -> None:
        """call_llm defaults to max_retries=2."""
        import inspect

        sig = inspect.signature(call_llm)
        assert sig.parameters["max_retries"].default == 2

    def test_call_llm_with_images_default_max_retries(self) -> None:
        """call_llm_with_images defaults to max_retries=2."""
        import inspect

        sig = inspect.signature(call_llm_with_images)
        assert sig.parameters["max_retries"].default == 2


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
        """Unknown provider returns None even when fallback keys are in env."""
        with patch.dict("os.environ", {}, clear=True):
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
        with patch.dict("os.environ", {}, clear=True):
            assert create_client(provider="openai") is None


# ---------------------------------------------------------------------------
# Transient-error retries with the REAL google-genai exception types (#5056)
# ---------------------------------------------------------------------------
#
# Before #5056 these tests raised made-up classes named
# ``ServiceUnavailableError`` / ``ResourceExhaustedError``, which matched the
# old class-name check, while google-genai actually raises
# ``google.genai.errors.ClientError`` / ``ServerError``, which did not: no
# Gemini error was ever retried in production.  Every test here builds the
# SDK's own exception classes, the way the SDK does
# (``APIError.raise_error(code, response_json, response)``).


def _genai_error(
    code: int, status: str, *, details: list | None = None, headers: dict | None = None
) -> genai_errors.APIError:
    """A real ``google.genai.errors`` exception for HTTP *code*."""
    body: dict = {"error": {"code": code, "message": f"{status} message", "status": status}}
    if details is not None:
        body["error"]["details"] = details
    response = httpx.Response(code, headers=headers or {}, json=body)
    try:
        genai_errors.APIError.raise_error(code, body, response)
    except genai_errors.APIError as exc:
        return exc
    raise AssertionError("raise_error did not raise")  # pragma: no cover


def _ok_response(text: str = "ok") -> MagicMock:
    usage = MagicMock()
    usage.prompt_token_count = 10
    usage.candidates_token_count = 5
    response = MagicMock()
    response.text = text
    response.usage_metadata = usage
    response.candidates = []
    return response


@pytest.fixture
def no_sleep() -> Iterator[MagicMock]:
    with patch("ingestion.llm_retry.time.sleep") as mock_sleep:
        yield mock_sleep


class TestGenaiErrorsRetry:
    """``_call_google`` / ``_call_google_with_images`` retry real SDK errors."""

    def test_genai_errors_are_the_sdk_classes(self) -> None:
        assert type(_genai_error(503, "UNAVAILABLE")) is genai_errors.ServerError
        assert type(_genai_error(429, "RESOURCE_EXHAUSTED")) is genai_errors.ClientError

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            (429, "RESOURCE_EXHAUSTED"),
            (500, "INTERNAL"),
            (502, "BAD_GATEWAY"),
            (503, "UNAVAILABLE"),
            (504, "DEADLINE_EXCEEDED"),
        ],
    )
    def test_genai_errors_transient_codes_are_retried(
        self, code: int, status: str, no_sleep: MagicMock
    ) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [_genai_error(code, status), _ok_response()]

        result = _call_google("sys", "user", "test-model", client=client, max_retries=2)

        assert result is not None
        assert result.text == "ok"
        assert client.models.generate_content.call_count == 2
        assert no_sleep.call_count == 1

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            (400, "INVALID_ARGUMENT"),
            (401, "UNAUTHENTICATED"),
            (403, "PERMISSION_DENIED"),
            (404, "NOT_FOUND"),
        ],
    )
    def test_genai_errors_client_errors_are_not_retried(
        self, code: int, status: str, no_sleep: MagicMock
    ) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = _genai_error(code, status)

        result = _call_google("sys", "user", "test-model", client=client, max_retries=3)

        assert result is None
        assert client.models.generate_content.call_count == 1
        no_sleep.assert_not_called()

    def test_genai_errors_vision_503_then_success(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [
            _genai_error(503, "UNAVAILABLE"),
            _genai_error(504, "DEADLINE_EXCEEDED"),
            _ok_response("page text"),
        ]

        result = _call_google_with_images(
            "sys", "user", [(b"img", "image/png")], "test-model", client=client, max_retries=2
        )

        assert result is not None
        assert result.text == "page text"
        assert client.models.generate_content.call_count == 3

    def test_genai_errors_vision_400_not_retried(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = _genai_error(400, "INVALID_ARGUMENT")

        result = _call_google_with_images(
            "sys", "user", [(b"img", "image/png")], "test-model", client=client, max_retries=2
        )

        assert result is None
        assert client.models.generate_content.call_count == 1

    def test_genai_errors_exhausted_returns_none(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = _genai_error(503, "UNAVAILABLE")

        result = _call_google("sys", "user", "test-model", client=client, max_retries=2)

        assert result is None
        assert client.models.generate_content.call_count == 3  # 1 + 2 retries

    def test_genai_errors_timeouts_and_resets_are_retried(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [
            httpx.ReadTimeout("timed out"),
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            ConnectionResetError(104, "Connection reset by peer"),
            _ok_response(),
        ]

        result = _call_google("sys", "user", "test-model", client=client, max_retries=3)

        assert result is not None
        assert client.models.generate_content.call_count == 4

    def test_genai_errors_retry_logs_events(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [
            _genai_error(429, "RESOURCE_EXHAUSTED"),
            _genai_error(503, "UNAVAILABLE"),
            _ok_response(),
        ]

        with structlog.testing.capture_logs() as logs:
            _call_google("sys", "user", "test-model", client=client, max_retries=2)

        events = [(e["event"], e.get("status")) for e in logs]
        assert ("llm_providers.google_rate_limit", 429) in events
        assert ("llm_providers.google_unavailable", 503) in events

    def test_genai_errors_vision_retry_log_prefix(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [
            _genai_error(504, "DEADLINE_EXCEEDED"),
            _ok_response(),
        ]

        with structlog.testing.capture_logs() as logs:
            _call_google_with_images(
                "sys", "u", [(b"i", "image/png")], "test-model", client=client, max_retries=1
            )

        assert [e["event"] for e in logs] == ["llm_providers.google_vision_unavailable"]

    def test_genai_errors_final_failure_logs_status(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = _genai_error(400, "INVALID_ARGUMENT")

        with structlog.testing.capture_logs() as logs:
            _call_google("sys", "user", "test-model", client=client)

        (entry,) = logs
        assert entry["event"] == "llm_providers.google_api_error"
        assert entry["status"] == 400
        assert entry["retryable"] is False

    def test_genai_errors_final_timeout_logs_timeout_event(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = httpx.ReadTimeout("timed out")

        with structlog.testing.capture_logs() as logs:
            result = _call_google(
                "sys", "user", "test-model", client=client, max_retries=0, timeout=5.0
            )

        assert result is None
        assert logs[-1]["event"] == "llm_providers.google_timeout"

    def test_genai_errors_retry_after_header_is_honoured(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [
            _genai_error(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "7"}),
            _ok_response(),
        ]

        result = _call_google("sys", "user", "test-model", client=client, max_retries=1)

        assert result is not None
        no_sleep.assert_called_once_with(7.0)

    def test_genai_errors_retry_info_delay_is_honoured(self, no_sleep: MagicMock) -> None:
        retry_info = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5s"}
        client = MagicMock()
        client.models.generate_content.side_effect = [
            _genai_error(429, "RESOURCE_EXHAUSTED", details=[retry_info]),
            _ok_response(),
        ]

        _call_google("sys", "user", "test-model", client=client, max_retries=1)

        no_sleep.assert_called_once_with(5.0)

    def test_genai_errors_deadline_stops_retries(self, no_sleep: MagicMock) -> None:
        """A retry that would not finish before the deadline is not made."""
        client = MagicMock()
        client.models.generate_content.side_effect = _genai_error(503, "UNAVAILABLE")

        result = _call_google_with_images(
            "sys",
            "user",
            [(b"img", "image/png")],
            "test-model",
            client=client,
            max_retries=5,
            timeout=30.0,
            deadline=time.monotonic() + 1.2,
        )

        assert result is None
        assert client.models.generate_content.call_count == 1
        no_sleep.assert_not_called()

    def test_genai_errors_past_deadline_makes_no_call(self, no_sleep: MagicMock) -> None:
        client = MagicMock()

        result = _call_google(
            "sys", "user", "test-model", client=client, deadline=time.monotonic() - 1
        )

        assert result is None
        client.models.generate_content.assert_not_called()

    def test_genai_errors_attempt_timeout_shortened_to_deadline(self, no_sleep: MagicMock) -> None:
        client = MagicMock()
        client.models.generate_content.return_value = _ok_response()

        _call_google(
            "sys",
            "user",
            "test-model",
            client=client,
            timeout=30.0,
            deadline=time.monotonic() + 5.0,
        )

        config = client.models.generate_content.call_args.kwargs["config"]
        assert 1000 <= config.http_options.timeout <= 5000

    def test_genai_errors_sdk_retry_stays_off(self, no_sleep: MagicMock) -> None:
        """Our loop is the only retry layer: no ``retry_options`` reach the SDK."""
        client = MagicMock()
        client.models.generate_content.return_value = _ok_response()

        _call_google("sys", "user", "test-model", client=client, timeout=10.0)

        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.http_options.retry_options is None

    def test_default_max_retries_is_2(self) -> None:
        import inspect

        assert inspect.signature(_call_google).parameters["max_retries"].default == 2
        assert inspect.signature(_call_google_with_images).parameters["max_retries"].default == 2


class TestGenaiErrorsRealTransport:
    """A real ``genai.Client`` over a stubbed HTTP transport: 503, then 200."""

    def test_genai_errors_real_client_retries_503(self, no_sleep: MagicMock) -> None:
        url = "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent"
        ok_body = {
            "candidates": [
                {"content": {"parts": [{"text": "ok"}], "role": "model"}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        }
        busy_body = {
            "error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}
        }
        with respx.mock(assert_all_called=True) as router:
            route = router.post(url).mock(
                side_effect=[httpx.Response(503, json=busy_body), httpx.Response(200, json=ok_body)]
            )
            client = genai.Client(api_key="fake-key")
            result = _call_google("sys", "user", "test-model", client=client, timeout=10.0)

        assert route.call_count == 2
        assert result is not None
        assert result.text == "ok"
        assert result.finish_reason == "STOP"
