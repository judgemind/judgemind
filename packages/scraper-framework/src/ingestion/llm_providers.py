"""Provider-agnostic LLM adapter layer.

Thin adapters for calling different LLM providers (Anthropic, Google GenAI)
with a unified interface. Each adapter accepts a system prompt + user message
and returns the raw text response + token counts.

Configuration is via environment variables:
- ``LLM_PROVIDER``: ``"google"`` (default) or ``"anthropic"``
- ``LLM_MODEL``: model ID (defaults per provider if not set)
- ``ANTHROPIC_API_KEY``: required when provider is ``"anthropic"``
- ``GOOGLE_API_KEY``: required when provider is ``"google"``
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import anthropic
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "google": "gemini-2.5-flash-lite",
}


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""

    text: str
    input_tokens: int
    output_tokens: int


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


def _call_anthropic(
    system_prompt: str,
    user_message: str,
    model: str,
    *,
    client: object | None = None,
    max_retries: int = 1,
    timeout: float | None = None,
    max_tokens: int = 4096,
) -> LLMResponse | None:
    """Call the Anthropic Messages API.

    Args:
        system_prompt: System prompt text.
        user_message: User message text.
        model: Anthropic model ID (e.g. ``"claude-haiku-4-5-20251001"``).
        client: Optional pre-created ``anthropic.Anthropic`` client for
            connection reuse.  If ``None``, creates one from the
            ``ANTHROPIC_API_KEY`` env var.
        max_retries: Number of retries on rate-limit errors.
        timeout: Per-call timeout in seconds.  If ``None``, no timeout
            is applied.  On timeout, returns ``None``.

    Returns:
        An ``LLMResponse`` or ``None`` on unrecoverable failure.
    """

    if client is None:
        try:
            client = anthropic.Anthropic()
        except anthropic.AuthenticationError:
            logger.warning("llm_providers.anthropic_auth_error")
            return None

    # Build optional timeout kwarg for the Anthropic SDK.
    timeout_kwargs: dict = {}
    if timeout is not None:
        timeout_kwargs["timeout"] = timeout

    for attempt in range(1 + max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                **timeout_kwargs,
            )
            return LLMResponse(
                text=response.content[0].text.strip(),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        except anthropic.APITimeoutError:
            logger.warning(
                "llm_providers.anthropic_timeout",
                timeout=timeout,
                attempt=attempt + 1,
            )
            return None
        except anthropic.RateLimitError:
            if attempt < max_retries:
                logger.warning("llm_providers.anthropic_rate_limit", attempt=attempt + 1)
                time.sleep(1)
                continue
            logger.warning("llm_providers.anthropic_rate_limit_exhausted")
            return None
        except anthropic.APIError as exc:
            logger.warning("llm_providers.anthropic_api_error", error=str(exc))
            return None
    return None  # pragma: no cover — defensive unreachable


def _call_google(
    system_prompt: str,
    user_message: str,
    model: str,
    *,
    client: object | None = None,
    max_retries: int = 1,
    timeout: float | None = None,
    max_tokens: int = 4096,
) -> LLMResponse | None:
    """Call the Google GenAI API.

    Args:
        system_prompt: System prompt text.
        user_message: User message text.
        model: Google model ID (e.g. ``"gemini-2.5-flash-lite"``).
        client: Optional pre-created ``google.genai.Client`` instance for
            connection reuse.  If ``None``, creates one from the
            ``GOOGLE_API_KEY`` env var.
        max_retries: Number of retries on resource-exhausted errors.
        timeout: Per-call timeout in seconds.  If ``None``, no timeout
            is applied.  On timeout, returns ``None``.

    Returns:
        An ``LLMResponse`` or ``None`` on unrecoverable failure.
    """
    from google import genai
    from google.genai import types

    if client is None:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            logger.warning("llm_providers.google_missing_api_key")
            return None
        client = genai.Client(api_key=api_key)

    # Defensive check: verify the SDK client exposes the expected API.
    # The google-genai 1.x SDK provides client.models.generate_content(),
    # but namespace conflicts with google-generativeai or broken installs
    # can produce a Models object without this method.
    if not hasattr(client.models, "generate_content"):
        logger.error(
            "llm_providers.google_sdk_incompatible",
            models_type=type(client.models).__qualname__,
            hint="Ensure google-genai>=1.0,<2.0 is installed; google-generativeai must NOT be",
        )
        return None

    config_kwargs: dict = {
        "system_instruction": system_prompt,
        "temperature": 0,
        "max_output_tokens": max_tokens,
        "response_mime_type": "application/json",
    }
    if timeout is not None:
        config_kwargs["http_options"] = types.HttpOptions(timeout=int(timeout * 1000))
    config = types.GenerateContentConfig(**config_kwargs)

    for attempt in range(1 + max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_message,
                config=config,
            )
            input_tokens = 0
            output_tokens = 0
            if response.usage_metadata:
                input_tokens = response.usage_metadata.prompt_token_count or 0
                output_tokens = response.usage_metadata.candidates_token_count or 0

            return LLMResponse(
                text=response.text.strip() if response.text else "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            # google-genai raises google.api_core.exceptions for rate limits
            # and other API errors.  Catch broadly and check for retryable.
            exc_name = type(exc).__name__
            # Timeout errors — return immediately without retrying.
            if "DeadlineExceeded" in exc_name or "Timeout" in exc_name:
                logger.warning(
                    "llm_providers.google_timeout",
                    timeout=timeout,
                    error=str(exc),
                )
                return None
            if "ResourceExhausted" in exc_name and attempt < max_retries:
                logger.warning("llm_providers.google_rate_limit", attempt=attempt + 1)
                time.sleep(1)
                continue
            logger.warning("llm_providers.google_api_error", error=str(exc))
            return None
    return None  # pragma: no cover — defensive unreachable


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def call_llm(
    system_prompt: str,
    user_message: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    client: object | None = None,
    max_retries: int = 1,
    timeout: float | None = None,
    max_tokens: int = 4096,
) -> LLMResponse | None:
    """Call an LLM provider and return the response.

    This is the single entry point for all LLM calls.  The provider and model
    are resolved from explicit arguments, then from environment variables
    (``LLM_PROVIDER``, ``LLM_MODEL``), then from built-in defaults.

    Args:
        system_prompt: System prompt text.
        user_message: User message text.
        provider: Provider name — ``"google"`` or ``"anthropic"``.
            Falls back to ``LLM_PROVIDER`` env var, then ``"google"``.
        model: Model ID.  Falls back to ``LLM_MODEL`` env var, then a
            per-provider default.
        client: Optional pre-created provider client for connection reuse.
        max_retries: Number of retries on rate-limit errors.
        timeout: Per-call timeout in seconds.  If ``None``, no timeout
            is applied.  On timeout, returns ``None`` so the caller can
            fall back to the next extraction tier (e.g. regex).

    Returns:
        An ``LLMResponse`` with the text and token counts, or ``None``
        on any unrecoverable failure.
    """
    resolved_provider = provider or os.environ.get("LLM_PROVIDER", "google")
    resolved_model = (
        model
        or os.environ.get("LLM_MODEL")
        or _PROVIDER_DEFAULT_MODELS.get(resolved_provider, "claude-haiku-4-5-20251001")
    )

    if resolved_provider == "anthropic":
        return _call_anthropic(
            system_prompt,
            user_message,
            resolved_model,
            client=client,
            max_retries=max_retries,
            timeout=timeout,
            max_tokens=max_tokens,
        )
    elif resolved_provider == "google":
        return _call_google(
            system_prompt,
            user_message,
            resolved_model,
            client=client,
            max_retries=max_retries,
            timeout=timeout,
            max_tokens=max_tokens,
        )
    else:
        logger.error("llm_providers.unknown_provider", provider=resolved_provider)
        return None


def _try_create(provider: str) -> object | None:
    """Try to create an LLM client for a single provider.

    Args:
        provider: Provider name — ``"google"`` or ``"anthropic"``.

    Returns:
        A provider-specific client object, or ``None`` if credentials
        are missing or the provider is unknown.
    """
    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        return anthropic.Anthropic()
    elif provider == "google":
        from google import genai

        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return None
        return genai.Client(api_key=api_key)
    else:
        return None


def create_client(
    provider: str | None = None,
) -> object | None:
    """Create a reusable LLM client for connection pooling.

    Tries the requested provider first.  If its API key is missing, falls
    back to the other known provider and logs a warning so the mismatch
    is visible.

    Args:
        provider: Provider name.  Falls back to ``LLM_PROVIDER`` env var,
            then ``"google"``.

    Returns:
        A provider-specific client object, or ``None`` if no provider's
        credentials are available.
    """
    resolved_provider = provider or os.environ.get("LLM_PROVIDER", "google")

    # Try the primary provider first.
    client = _try_create(resolved_provider)
    if client is not None:
        return client

    # Fallback: try other known providers.
    for alt in _PROVIDER_DEFAULT_MODELS:
        if alt != resolved_provider:
            client = _try_create(alt)
            if client is not None:
                logger.warning(
                    "llm_providers.fallback",
                    configured=resolved_provider,
                    using=alt,
                )
                return client

    return None
