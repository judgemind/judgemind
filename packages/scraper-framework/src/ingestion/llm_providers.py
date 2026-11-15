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
from dataclasses import dataclass

import anthropic
import structlog
from anthropic import Anthropic as _AnthropicClient
from judgemind_config import DEFAULT_HAIKU_MODEL

from .llm_retry import KIND_TIMEOUT, call_with_retries, classify_error

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": DEFAULT_HAIKU_MODEL,
    "google": "gemini-2.5-flash-lite",
}


def resolve_provider(provider: str | None = None) -> str:
    """Resolve the active LLM provider name.

    Resolution order:

    1. Explicit ``provider`` argument.
    2. ``LLM_PROVIDER`` environment variable.
    3. Default ``"google"``.

    This is the single source of truth for "which provider will ``call_llm``
    actually use?" — callers that need to record or log the provider should
    use this helper rather than reimplementing the precedence locally.
    """
    return provider or os.environ.get("LLM_PROVIDER", "google")


def resolve_model(provider: str | None = None, model: str | None = None) -> str:
    """Resolve the active LLM model ID for the given provider.

    Resolution order matches ``call_llm``:

    1. Explicit ``model`` argument.
    2. ``LLM_MODEL`` environment variable.
    3. Per-provider default from ``_PROVIDER_DEFAULT_MODELS``.
    4. ``DEFAULT_HAIKU_MODEL`` (defensive fallback for unknown providers).

    Callers (e.g. the ingestion summarizer) that need to persist or log the
    model used should call this helper instead of duplicating the precedence
    logic.  ``call_llm`` itself uses the same resolution.
    """
    resolved_provider = resolve_provider(provider)
    return (
        model
        or os.environ.get("LLM_MODEL")
        or _PROVIDER_DEFAULT_MODELS.get(resolved_provider, DEFAULT_HAIKU_MODEL)
    )


# Finish reasons, normalized across providers to Gemini's names (#5007).
FINISH_STOP = "STOP"
FINISH_MAX_TOKENS = "MAX_TOKENS"
FINISH_RECITATION = "RECITATION"

# Anthropic ``stop_reason`` values mapped onto the Gemini names.
_ANTHROPIC_FINISH_REASONS: dict[str, str] = {
    "end_turn": FINISH_STOP,
    "stop_sequence": FINISH_STOP,
    "max_tokens": FINISH_MAX_TOKENS,
    "refusal": "SAFETY",
}


@dataclass
class LLMResponse:
    """Unified response from any LLM provider.

    ``finish_reason`` says why the model stopped, normalized to Gemini's
    names: ``"STOP"`` (a complete answer), ``"MAX_TOKENS"`` (cut off at the
    output budget), ``"RECITATION"`` / ``"SAFETY"`` / ``"OTHER"`` (the
    provider withheld the text), or ``None`` when the provider did not say.
    ``block_reason`` is set when the prompt itself was blocked, and
    ``safety_ratings`` lists each rating above ``NEGLIGIBLE`` or that
    blocked the answer.  Callers log :meth:`diagnostics` when a response
    is empty or does not parse, so a deterministic failure names its
    cause (#5007).
    """

    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str | None = None
    block_reason: str | None = None
    safety_ratings: list[str] | None = None
    # Tokens a thinking model spent reasoning (Gemini ``thoughts_token_count``),
    # which count against the output budget; ``None`` when it did not think
    # (#5046).
    thinking_tokens: int | None = None

    @property
    def truncated(self) -> bool:
        """True when the answer was cut off at the output-token budget."""
        return self.finish_reason == FINISH_MAX_TOKENS

    @property
    def incomplete(self) -> bool:
        """True when the provider did not finish the answer normally.

        A truncated or withheld answer is never a complete result, even
        when its text happens to parse: it must not be cached or written.
        """
        if self.block_reason:
            return True
        return self.finish_reason is not None and self.finish_reason != FINISH_STOP

    def diagnostics(self) -> dict[str, object]:
        """Fields to log with an empty, truncated, or unparseable response."""
        return {
            "finish_reason": self.finish_reason,
            "block_reason": self.block_reason,
            "safety_ratings": self.safety_ratings or [],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "response_chars": len(self.text),
        }


def response_diagnostics(response: object) -> dict[str, object]:
    """``response.diagnostics()``, or ``{}`` for an object without it.

    Safe to splat into a log call for any response-like object, including
    test doubles that are not an ``LLMResponse``.
    """
    if not isinstance(response, LLMResponse):
        return {}
    return response.diagnostics()


def _enum_name(value: object) -> str | None:
    """Return an SDK enum's name (``FinishReason.STOP`` -> ``"STOP"``)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.rsplit(".", 1)[-1] or None
    name = getattr(value, "name", None)
    return name if isinstance(name, str) and name else None


def _google_response(response: object) -> LLMResponse:
    """Build an ``LLMResponse`` from a google-genai ``GenerateContentResponse``.

    Reads the first candidate's ``finish_reason`` and ``safety_ratings`` and
    the prompt's ``block_reason``.  Every attribute is read defensively: a
    missing or malformed field leaves its value ``None`` instead of raising.
    """
    input_tokens = 0
    output_tokens = 0
    thinking_tokens: int | None = None
    usage = getattr(response, "usage_metadata", None)
    if usage:
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        thoughts = getattr(usage, "thoughts_token_count", None)
        thinking_tokens = thoughts if isinstance(thoughts, int) else None

    finish_reason: str | None = None
    safety: list[str] = []
    candidates = getattr(response, "candidates", None)
    if isinstance(candidates, (list, tuple)) and candidates:
        candidate = candidates[0]
        finish_reason = _enum_name(getattr(candidate, "finish_reason", None))
        ratings = getattr(candidate, "safety_ratings", None)
        if isinstance(ratings, (list, tuple)):
            for rating in ratings:
                probability = _enum_name(getattr(rating, "probability", None))
                blocked = getattr(rating, "blocked", None) is True
                if blocked or probability not in (None, "NEGLIGIBLE"):
                    category = _enum_name(getattr(rating, "category", None))
                    safety.append(f"{category}:{probability}{':blocked' if blocked else ''}")

    block_reason: str | None = None
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None:
        block_reason = _enum_name(getattr(feedback, "block_reason", None))
        if block_reason == "BLOCKED_REASON_UNSPECIFIED":
            block_reason = None

    try:
        raw_text = response.text  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — the SDK raises on some blocked responses
        raw_text = None
    text = raw_text.strip() if isinstance(raw_text, str) else ""

    return LLMResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        block_reason=block_reason,
        safety_ratings=safety or None,
        thinking_tokens=thinking_tokens,
    )


def _google_thinking_config(model: str, thinking_budget: int | None) -> object | None:
    """A ``ThinkingConfig`` capping a Gemini model's thinking, or ``None``.

    Thinking tokens count against ``max_output_tokens``: on a thinking model
    (gemini-2.5-flash thinks by default) a transcription can stop at
    ``MAX_TOKENS`` having written almost nothing (#5046).  Only the
    gemini-2.5-flash family accepts a budget of 0; other models get no
    config rather than a request they would reject.
    """
    if thinking_budget is None or not model.startswith("gemini-2.5-flash"):
        return None
    from google.genai import types

    return types.ThinkingConfig(thinking_budget=thinking_budget)


def _anthropic_finish_reason(response: object) -> str | None:
    """Normalize an Anthropic ``stop_reason`` to the Gemini finish names."""
    stop_reason = getattr(response, "stop_reason", None)
    if not isinstance(stop_reason, str):
        return None
    return _ANTHROPIC_FINISH_REASONS.get(stop_reason, stop_reason.upper())


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


def new_anthropic_client(**kwargs: object) -> anthropic.Anthropic:
    """An ``anthropic.Anthropic`` client with the SDK's own retry off.

    The SDK retries 408/409/429/5xx and dropped connections twice by
    default.  ``llm_retry.call_with_retries`` is our one retry layer
    (deadline-aware, honours ``Retry-After``), so every client we build
    passes ``max_retries=0`` and nothing retries twice (#5066).
    """
    return anthropic.Anthropic(max_retries=0, **kwargs)  # type: ignore[arg-type]


def without_sdk_retries(client: object) -> object:
    """*client* with the Anthropic SDK's own retry turned off.

    A caller may hand in a client it built itself with the SDK default
    ``max_retries=2``; ``with_options`` returns a copy sharing the same
    connection pool, with ``max_retries=0``.  Anything that is not a real
    ``anthropic.Anthropic`` (a test double) is returned unchanged.
    """
    if not isinstance(client, _AnthropicClient):
        return client
    retries = getattr(client, "max_retries", None)  # absent on a spec'd mock
    if isinstance(retries, int) and retries > 0:
        return client.with_options(max_retries=0)
    return client


def anthropic_messages_create(
    client: object,
    request: dict,
    *,
    max_retries: int,
    timeout: float | None,
    deadline: float | None,
    event_prefix: str,
) -> object:
    """``client.messages.create(**request)`` under the shared retry policy.

    Transient errors (429 / 5xx / 529 / timeouts / dropped connections)
    are retried by ``llm_retry.call_with_retries``, within *deadline*;
    each attempt's HTTP timeout is *timeout* shortened to the time left
    (no ``timeout`` keyword is sent when neither is set).  The SDK's own
    retry is off (:func:`without_sdk_retries`).  Returns the SDK
    ``Message``; the last exception propagates when every attempt failed.
    """
    sdk_client = without_sdk_retries(client)

    def attempt(attempt_timeout: float | None) -> object:
        kwargs = dict(request)
        if attempt_timeout is not None:
            kwargs["timeout"] = attempt_timeout
        return sdk_client.messages.create(**kwargs)  # type: ignore[attr-defined]

    return call_with_retries(
        attempt,
        max_retries=max_retries,
        timeout=timeout,
        deadline=deadline,
        event_prefix=event_prefix,
    )


def _log_final_failure(event_prefix: str, exc: Exception, timeout: float | None) -> None:
    """Log ``<event_prefix>timeout`` or ``<event_prefix>api_error``."""
    decision = classify_error(exc)
    event = "timeout" if decision.kind == KIND_TIMEOUT else "api_error"
    logger.warning(
        f"{event_prefix}{event}",
        timeout=timeout,
        status=decision.status,
        kind=decision.kind,
        retryable=decision.retryable,
        error=str(exc),
    )


def _call_anthropic(
    system_prompt: str,
    user_message: str,
    model: str,
    *,
    client: object | None = None,
    max_retries: int = 2,
    timeout: float | None = None,
    max_tokens: int = 4096,
    deadline: float | None = None,
) -> LLMResponse | None:
    """Call the Anthropic Messages API.

    Args:
        system_prompt: System prompt text.
        user_message: User message text.
        model: Anthropic model ID (e.g. ``"claude-haiku-4-5-20251001"``).
        client: Optional pre-created ``anthropic.Anthropic`` client for
            connection reuse.  If ``None``, creates one from the
            ``ANTHROPIC_API_KEY`` env var.
        max_retries: Number of retries on transient errors (429, 5xx, 529,
            timeouts, dropped connections; see ``ingestion.llm_retry``),
            with exponential backoff and jitter.
        timeout: Per-attempt timeout in seconds.  If ``None``, the SDK
            default applies.
        deadline: ``time.monotonic()`` value no attempt or retry may run
            past.  If ``None``, only *max_retries* bounds the retries.

    Returns:
        An ``LLMResponse`` or ``None`` on unrecoverable failure.
    """
    event_prefix = "llm_providers.anthropic_"
    try:
        if client is None:
            client = new_anthropic_client()
        response = anthropic_messages_create(
            client,
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
            max_retries=max_retries,
            timeout=timeout,
            deadline=deadline,
            event_prefix=event_prefix,
        )
        return LLMResponse(
            text=response.content[0].text.strip(),  # type: ignore[attr-defined]
            input_tokens=response.usage.input_tokens,  # type: ignore[attr-defined]
            output_tokens=response.usage.output_tokens,  # type: ignore[attr-defined]
            finish_reason=_anthropic_finish_reason(response),
        )
    except Exception as exc:  # noqa: BLE001 — every SDK failure means "no answer"
        _log_final_failure(event_prefix, exc, timeout)
        return None


def _call_google(
    system_prompt: str,
    user_message: str,
    model: str,
    *,
    client: object | None = None,
    max_retries: int = 2,
    timeout: float | None = None,
    max_tokens: int = 4096,
    deadline: float | None = None,
) -> LLMResponse | None:
    """Call the Google GenAI API.

    Args:
        system_prompt: System prompt text.
        user_message: User message text.
        model: Google model ID (e.g. ``"gemini-2.5-flash-lite"``).
        client: Optional pre-created ``google.genai.Client`` instance for
            connection reuse.  If ``None``, creates one from the
            ``GOOGLE_API_KEY`` env var.
        max_retries: Number of retries on transient errors (429, 5xx,
            timeouts, dropped connections; see ``ingestion.llm_retry``),
            with exponential backoff and jitter.
        timeout: Per-attempt timeout in seconds.  If ``None``, no timeout
            is applied.
        deadline: ``time.monotonic()`` value no attempt or retry may run
            past.  If ``None``, only *max_retries* bounds the retries.

    Returns:
        An ``LLMResponse`` or ``None`` on unrecoverable failure.
    """
    from google import genai

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
    return _google_generate(
        client,
        model,
        user_message,
        config_kwargs,
        max_retries=max_retries,
        timeout=timeout,
        deadline=deadline,
        event_prefix="llm_providers.google_",
    )


def _google_generate(
    client: object,
    model: str,
    contents: object,
    config_kwargs: dict,
    *,
    max_retries: int,
    timeout: float | None,
    deadline: float | None,
    event_prefix: str,
) -> LLMResponse | None:
    """One ``generate_content`` call with the shared retry policy (#5056).

    Transient errors (429 / 5xx / timeouts / dropped connections) are
    retried by ``llm_retry.call_with_retries``, within *deadline*; each
    attempt's HTTP timeout is *timeout* shortened to the time left.  The
    SDK's own retry stays off (no ``retry_options``), so nothing retries
    twice.  On final failure, logs ``<event_prefix>timeout`` or
    ``<event_prefix>api_error`` and returns ``None``.
    """
    from google.genai import types

    def attempt(attempt_timeout: float | None) -> LLMResponse:
        kwargs = dict(config_kwargs)
        if attempt_timeout is not None:
            kwargs["http_options"] = types.HttpOptions(timeout=max(1, int(attempt_timeout * 1000)))
        response = client.models.generate_content(  # type: ignore[attr-defined]
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(**kwargs),
        )
        return _google_response(response)

    try:
        return call_with_retries(
            attempt,
            max_retries=max_retries,
            timeout=timeout,
            deadline=deadline,
            event_prefix=event_prefix,
        )
    except Exception as exc:  # noqa: BLE001 — every SDK failure means "no answer"
        _log_final_failure(event_prefix, exc, timeout)
        return None


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
    max_retries: int = 2,
    timeout: float | None = None,
    max_tokens: int = 4096,
    deadline: float | None = None,
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
        max_retries: Number of retries on transient errors, for every
            provider: ``ingestion.llm_retry`` decides (429, 500, 502, 503,
            504, 529, timeouts and dropped connections).
        timeout: Per-attempt timeout in seconds.  If ``None``, no timeout
            is applied.  When every attempt fails, returns ``None`` so the
            caller can fall back to the next extraction tier (e.g. regex).
        deadline: ``time.monotonic()`` value no attempt or retry may run
            past.

    Returns:
        An ``LLMResponse`` with the text and token counts, or ``None``
        on any unrecoverable failure.
    """
    resolved_provider = resolve_provider(provider)
    resolved_model = resolve_model(resolved_provider, model)

    if resolved_provider == "anthropic":
        return _call_anthropic(
            system_prompt,
            user_message,
            resolved_model,
            client=client,
            max_retries=max_retries,
            timeout=timeout,
            max_tokens=max_tokens,
            deadline=deadline,
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
            deadline=deadline,
        )
    else:
        logger.error("llm_providers.unknown_provider", provider=resolved_provider)
        return None


def call_llm_with_images(
    system_prompt: str,
    text_message: str,
    images: list[tuple[bytes, str]],
    *,
    provider: str | None = None,
    model: str | None = None,
    client: object | None = None,
    max_retries: int = 2,
    timeout: float | None = None,
    max_tokens: int = 4096,
    json_mode: bool = False,
    thinking_budget: int | None = None,
    deadline: float | None = None,
) -> LLMResponse | None:
    """Call an LLM provider with text and image content (vision API).

    Similar to ``call_llm`` but includes inline images in the request.
    Used for OCR fallback when PDF pages have no text layer and must be
    "read" visually by the model.

    Args:
        system_prompt: System prompt text.
        text_message: Text portion of the user message.
        images: List of ``(image_bytes, media_type)`` tuples.
            ``media_type`` is e.g. ``"image/png"`` or ``"image/jpeg"``.
        provider: Provider name — ``"google"`` or ``"anthropic"``.
        model: Model ID.
        client: Optional pre-created provider client.
        max_retries: Number of retries on transient errors, for every
            provider: ``ingestion.llm_retry`` decides (429, 500, 502, 503,
            504, 529, timeouts and dropped connections).
        timeout: Per-attempt timeout in seconds.
        deadline: ``time.monotonic()`` value no attempt or retry may run
            past (the OCR run budget, #4986), for every provider.
        max_tokens: Maximum output tokens.
        json_mode: Ask Google for a JSON response
            (``response_mime_type="application/json"``), which constrains
            the output to valid JSON.  Ignored for Anthropic.
        thinking_budget: Cap on a Gemini thinking model's reasoning tokens
            (``0`` turns thinking off; see ``_google_thinking_config``).
            ``None`` keeps the model's default.  Ignored for Anthropic,
            which does not think unless asked to.

    Returns:
        An ``LLMResponse`` or ``None`` on failure.
    """
    resolved_provider = resolve_provider(provider)
    resolved_model = resolve_model(resolved_provider, model)

    if resolved_provider == "anthropic":
        return _call_anthropic_with_images(
            system_prompt,
            text_message,
            images,
            resolved_model,
            client=client,
            max_retries=max_retries,
            timeout=timeout,
            max_tokens=max_tokens,
            deadline=deadline,
        )
    elif resolved_provider == "google":
        return _call_google_with_images(
            system_prompt,
            text_message,
            images,
            resolved_model,
            client=client,
            max_retries=max_retries,
            timeout=timeout,
            max_tokens=max_tokens,
            json_mode=json_mode,
            thinking_budget=thinking_budget,
            deadline=deadline,
        )
    else:
        logger.error("llm_providers.unknown_provider", provider=resolved_provider)
        return None


def _call_anthropic_with_images(
    system_prompt: str,
    text_message: str,
    images: list[tuple[bytes, str]],
    model: str,
    *,
    client: object | None = None,
    max_retries: int = 2,
    timeout: float | None = None,
    max_tokens: int = 4096,
    deadline: float | None = None,
) -> LLMResponse | None:
    """Call the Anthropic Messages API with image content blocks.

    Retries and *deadline* work as in :func:`_call_anthropic`.
    """
    import base64

    # Build content blocks: images first, then text
    content: list[dict] = []
    for image_bytes, media_type in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": text_message})

    event_prefix = "llm_providers.anthropic_vision_"
    try:
        if client is None:
            client = new_anthropic_client()
        response = anthropic_messages_create(
            client,
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "system": system_prompt,
                "messages": [{"role": "user", "content": content}],
            },
            max_retries=max_retries,
            timeout=timeout,
            deadline=deadline,
            event_prefix=event_prefix,
        )
    except Exception as exc:  # noqa: BLE001 — every SDK failure means "no answer"
        _log_final_failure(event_prefix, exc, timeout)
        return None
    # Vision responses may contain non-text blocks; find the first
    # text block rather than assuming content[0] is text.
    text_content = ""
    for block in response.content:  # type: ignore[attr-defined]
        if hasattr(block, "type") and block.type == "text":
            text_content = block.text
            break
    return LLMResponse(
        text=text_content.strip(),
        input_tokens=response.usage.input_tokens,  # type: ignore[attr-defined]
        output_tokens=response.usage.output_tokens,  # type: ignore[attr-defined]
        finish_reason=_anthropic_finish_reason(response),
    )


def _call_google_with_images(
    system_prompt: str,
    text_message: str,
    images: list[tuple[bytes, str]],
    model: str,
    *,
    client: object | None = None,
    max_retries: int = 2,
    timeout: float | None = None,
    max_tokens: int = 4096,
    json_mode: bool = False,
    thinking_budget: int | None = None,
    deadline: float | None = None,
) -> LLMResponse | None:
    """Call the Google GenAI API with image content (multimodal).

    Retries and *deadline* work as in :func:`_call_google`.
    """
    from google import genai
    from google.genai import types

    if client is None:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            logger.warning("llm_providers.google_missing_api_key")
            return None
        client = genai.Client(api_key=api_key)

    if not hasattr(client.models, "generate_content"):
        logger.error(
            "llm_providers.google_sdk_incompatible",
            models_type=type(client.models).__qualname__,
        )
        return None

    # Build multimodal content: images + text
    parts: list[types.Part] = []
    for image_bytes, media_type in images:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=media_type))
    parts.append(types.Part.from_text(text=text_message))

    config_kwargs: dict = {
        "system_instruction": system_prompt,
        "temperature": 0,
        "max_output_tokens": max_tokens,
    }
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"
    thinking_config = _google_thinking_config(model, thinking_budget)
    if thinking_config is not None:
        config_kwargs["thinking_config"] = thinking_config
    return _google_generate(
        client,
        model,
        parts,
        config_kwargs,
        max_retries=max_retries,
        timeout=timeout,
        deadline=deadline,
        event_prefix="llm_providers.google_vision_",
    )


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
        return new_anthropic_client()
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
    resolved_provider = resolve_provider(provider)

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
