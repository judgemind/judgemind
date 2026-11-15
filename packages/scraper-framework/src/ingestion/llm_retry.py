"""Retry policy for transient LLM-provider errors (#5056, #5066).

Every Gemini and Anthropic call in the pipeline goes through
:func:`call_with_retries` (via ``ingestion.llm_providers`` and
``framework.llm_extractor``), so this module is the one place that decides
which errors are worth another attempt and how long to wait.

Retryable:

- ``google.genai.errors.APIError`` (``ClientError`` / ``ServerError``)
  whose HTTP ``code`` is 429, 500, 502, 503 or 504;
- ``anthropic.APIStatusError`` (``RateLimitError``,
  ``InternalServerError``, ``OverloadedError``, ...) whose
  ``status_code`` is 429, 500, 502, 503, 504 or 529 (Anthropic's
  "overloaded");
- transport timeouts (``httpx.TimeoutException``, ``TimeoutError``,
  ``anthropic.APITimeoutError``);
- dropped connections (``httpx.NetworkError``,
  ``httpx.RemoteProtocolError``, ``ConnectionError``,
  ``anthropic.APIConnectionError``).

Everything else (400, 401, 403, 404, programming errors) fails on the
first attempt.  Content-level outcomes (a ``RECITATION`` / ``SAFETY``
finish reason, a ``MAX_TOKENS`` cut-off) are not exceptions at all: they
come back as an ``LLMResponse`` and are handled by the callers (#5007,
#5046), never retried here.

The waits are exponential with jitter, raised to the server's
``Retry-After`` / ``retry-after-ms`` header or ``RetryInfo.retryDelay``
when it gives one.  A retry is never started when the wait would run past
the caller's *deadline* (a ``time.monotonic()`` value, e.g. the OCR run
budget), and each attempt's HTTP timeout is shortened to the time left, so
the deadline holds.

The SDKs' own retries are deliberately off: google-genai's
``HttpOptions.retry_options`` is left unset (its default), and every
Anthropic client is built with ``max_retries=0`` (the SDK default is 2).
They know nothing about our deadlines and would multiply with this loop.
Before #5056 the Gemini check matched ``google.api_core`` class names
(``ResourceExhausted``, ``ServiceUnavailable``) that google-genai never
raises, so no Gemini error was ever retried; before #5066 one Anthropic 529
could cost 3 SDK attempts times 3 of our own.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import anthropic
import httpx
import structlog
from google.genai import errors as genai_errors

logger = structlog.get_logger(__name__)

# HTTP status codes worth another attempt: rate limit and server-side
# failures (504 is Gemini's DEADLINE_EXCEEDED, "please try again"; 529 is
# Anthropic's "overloaded").
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504, 529})

# First backoff step (seconds); attempt n waits about BASE * 2**n.
BASE_DELAY = 1.0

# No single wait is longer than this.  A server asking for a longer pause
# (a quota window, not a blip) is not waited for: the call fails now.
MAX_DELAY = 30.0

# An attempt with less time than this left before the deadline is not
# started: it could not finish.
MIN_ATTEMPT_SECONDS = 1.0

KIND_RATE_LIMIT = "rate_limit"
KIND_SERVER_ERROR = "server_error"
KIND_TIMEOUT = "timeout"
KIND_CONNECTION = "connection"
KIND_CLIENT_ERROR = "client_error"
KIND_OTHER = "other"


@dataclass(frozen=True)
class RetryDecision:
    """How :func:`classify_error` sees one exception."""

    retryable: bool
    kind: str
    status: int | None = None
    retry_after: float | None = None


def classify_error(exc: BaseException) -> RetryDecision:
    """Classify *exc* by its real type and HTTP status code."""
    if isinstance(exc, genai_errors.APIError):
        code = exc.code if isinstance(exc.code, int) else None
        return _status_decision(code, exc)
    if isinstance(exc, anthropic.APIStatusError):
        code = exc.status_code if isinstance(exc.status_code, int) else None
        return _status_decision(code, exc)
    # APITimeoutError subclasses APIConnectionError, so it is checked first.
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, anthropic.APITimeoutError)):
        return RetryDecision(True, KIND_TIMEOUT)
    if isinstance(
        exc,
        (
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            ConnectionError,
            anthropic.APIConnectionError,
        ),
    ):
        return RetryDecision(True, KIND_CONNECTION)
    return RetryDecision(False, KIND_OTHER)


def _status_decision(code: int | None, exc: BaseException) -> RetryDecision:
    """Decide on an HTTP-status error from either SDK."""
    if code in RETRYABLE_STATUS_CODES:
        kind = KIND_RATE_LIMIT if code == 429 else KIND_SERVER_ERROR
        return RetryDecision(True, kind, code, _retry_after(exc))
    kind = KIND_CLIENT_ERROR if code is not None and 400 <= code < 500 else KIND_OTHER
    return RetryDecision(False, kind, code)


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$")


def _seconds(value: object) -> float | None:
    """``"7"``, ``"7s"`` or ``"1.5s"`` as seconds; ``None`` if unparseable."""
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value)
    return float(match.group(1)) if match else None


def _retry_after(exc: BaseException) -> float | None:
    """The server's requested pause: a ``retry-after-ms`` (Anthropic) or
    ``Retry-After`` header, else the ``google.rpc.RetryInfo``
    ``retryDelay`` in a google-genai error body."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            millis = _seconds(headers.get("retry-after-ms"))
            seconds = millis / 1000 if millis is not None else _seconds(headers.get("retry-after"))
        except Exception:  # noqa: BLE001 — a header object we do not know
            seconds = None
        if seconds is not None:
            return seconds
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        details = details.get("error", details)
    items = details.get("details") if isinstance(details, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            seconds = _seconds(item.get("retryDelay"))
            if seconds is not None:
                return seconds
    return None


def backoff_delay(
    attempt: int,
    retry_after: float | None = None,
    *,
    rand: Callable[[], float] = random.random,
) -> float:
    """Wait before retry number *attempt* + 1 (*attempt* counts from 0).

    Exponential (``BASE_DELAY * 2**attempt``) with equal jitter (half the
    step fixed, half random), at least *retry_after*, at most ``MAX_DELAY``.
    """
    step = BASE_DELAY * (2**attempt)
    delay = step / 2 + rand() * step / 2
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(delay, MAX_DELAY)


def call_with_retries[T](
    fn: Callable[[float | None], T],
    *,
    max_retries: int,
    timeout: float | None = None,
    deadline: float | None = None,
    event_prefix: str,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> T:
    """``fn(attempt_timeout)``, retried on transient errors.

    *fn* gets the HTTP timeout for this attempt: *timeout*, shortened to
    the time left before *deadline* (``None`` when neither is set).  At most
    *max_retries* retries follow the first attempt.  Each retry logs
    ``<event_prefix>rate_limit`` (429) or ``<event_prefix>unavailable``
    (5xx, timeouts, dropped connections).  The last exception is re-raised
    when the error is not retryable, the retries are used up, or the next
    attempt would not fit before *deadline*; a :class:`TimeoutError` is
    raised when no attempt fits at all.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    attempt = 0
    while True:
        attempt_timeout = timeout
        if deadline is not None:
            remaining = deadline - clock()
            if remaining < MIN_ATTEMPT_SECONDS:
                raise TimeoutError(f"LLM call deadline reached before attempt {attempt + 1}")
            attempt_timeout = remaining if timeout is None else min(timeout, remaining)
        try:
            return fn(attempt_timeout)
        except Exception as exc:
            decision = classify_error(exc)
            if not decision.retryable or attempt >= max_retries:
                raise
            if decision.retry_after is not None and decision.retry_after > MAX_DELAY:
                raise
            wait = backoff_delay(attempt, decision.retry_after)
            if deadline is not None and clock() + wait + MIN_ATTEMPT_SECONDS > deadline:
                raise
            event = "rate_limit" if decision.kind == KIND_RATE_LIMIT else "unavailable"
            logger.warning(
                f"{event_prefix}{event}",
                attempt=attempt + 1,
                max_retries=max_retries,
                retry_in=round(wait, 2),
                status=decision.status,
                kind=decision.kind,
                error=str(exc)[:300],
            )
            sleep(wait)
            attempt += 1
