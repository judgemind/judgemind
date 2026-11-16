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
first attempt.

:data:`PROVIDER_ERRORS` (:func:`is_provider_error`) is the one definition
of "the provider or the network failed": every ``genai_errors.APIError``
and ``anthropic.APIError`` (retryable or not), ``httpx`` timeouts and
transport errors, ``TimeoutError`` (also what :func:`call_with_retries`
raises when the deadline leaves no room) and ``ConnectionError``.  Only
these mean "no answer" to the provider helpers.  Anything else
(``TypeError``, ``AttributeError``, ``KeyError``, ...) is a bug in our code
or config and must propagate (#5125): before that, a ``TypeError`` from a
rejected keyword read as an outage for 631 calls (#4960).

Content-level outcomes (a ``RECITATION`` / ``SAFETY``
finish reason, a ``MAX_TOKENS`` cut-off) are not exceptions at all: they
come back as an ``LLMResponse`` and are handled by the callers (#5007,
#5046), never retried here.

The waits are exponential with jitter (base 2 s, capped at 30 s), raised
to the server's ``Retry-After`` / ``retry-after-ms`` header or
``RetryInfo.retryDelay`` when it gives one.  A server retry-after is
honoured up to 65 s (Gemini per-minute quota windows); a longer one (a
daily quota) fails the call at once.  Congestion statuses (429, 503, 529)
get at least 4 retries when the caller allows any (#5096).  The total
slept in one call is capped at 75 s, deadline or not, so callers without a
deadline stay bounded.  A retry is never started when the wait would run
past the caller's *deadline* (a ``time.monotonic()`` value, e.g. the OCR
run budget), and each attempt's HTTP timeout is shortened to the time left,
so the deadline holds.

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

# First backoff step (seconds); attempt n waits BASE * 2**n with equal
# jitter: 1-2 s, 2-4 s, 4-8 s, 8-16 s (#5096: at 1 s a 529 episode got
# only ~3 s of retries).
BASE_DELAY = 2.0

# Cap on the computed exponential backoff only.  A server retry-after is
# bounded by MAX_RETRY_AFTER instead.
MAX_DELAY = 30.0

# A server retry-after up to this is honoured: Gemini per-minute quota
# windows reset within 60 s (5 s margin).  A longer retry-after (e.g. a
# daily quota window) is not waited for: the call fails now, zero retries.
MAX_RETRY_AFTER = 65.0

# Congestion statuses (rate limit 429, Gemini's "model is overloaded" 503
# UNAVAILABLE, Anthropic's "overloaded" 529) get at least this many retries
# whenever the caller allows any (max_retries > 0).  Other retryable errors
# keep the caller's max_retries.
CONGESTION_STATUS_CODES: frozenset[int] = frozenset({429, 503, 529})
CONGESTION_MIN_RETRIES = 4

# Cumulative seconds one call_with_retries call may sleep, deadline or not.
# Callers without a deadline (llm_extractor chunk calls) are otherwise
# bounded only by retries x waits (4 x 65 s).  75 s fits one full 65 s
# quota wait plus a few backoffs, and stays under the worker's 105 s SIGTERM
# grace (shutdown.SHUTDOWN_GRACE_S), so a call that is waiting when the stop
# arrives can usually still finish.  The message lease is renewed by a
# separate heartbeat thread (worker.py), so sleeping here never loses it;
# the cap is about not wasting the grace window and not stalling a message
# for minutes.
MAX_TOTAL_WAIT = 75.0

# An attempt with less time than this left before the deadline is not
# started: it could not finish.
MIN_ATTEMPT_SECONDS = 1.0

KIND_RATE_LIMIT = "rate_limit"
KIND_SERVER_ERROR = "server_error"
KIND_TIMEOUT = "timeout"
KIND_CONNECTION = "connection"
KIND_CLIENT_ERROR = "client_error"
KIND_OTHER = "other"

# SDK API errors and transport failures: the provider or the network did
# not answer.  ``anthropic.APIError`` covers ``APIStatusError``,
# ``APIConnectionError``, ``APITimeoutError`` and
# ``APIResponseValidationError``; it does not cover ``AnthropicError``
# itself (e.g. a missing API key), which is a config error.  Everything
# outside this tuple is a bug and must propagate (#5125).
PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    genai_errors.APIError,
    anthropic.APIError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    TimeoutError,
    ConnectionError,
)


def is_provider_error(exc: BaseException) -> bool:
    """True when *exc* is a provider / network failure (:data:`PROVIDER_ERRORS`)."""
    return isinstance(exc, PROVIDER_ERRORS)


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
    step fixed, half random), capped at ``MAX_DELAY``; then raised to
    *retry_after* (itself capped at ``MAX_RETRY_AFTER``) when one is given.
    """
    step = BASE_DELAY * (2**attempt)
    delay = min(step / 2 + rand() * step / 2, MAX_DELAY)
    if retry_after is not None:
        delay = max(delay, min(retry_after, MAX_RETRY_AFTER))
    return delay


def _retry_limit(max_retries: int, decision: RetryDecision) -> int:
    """Retries allowed for this error: congestion statuses get at least
    ``CONGESTION_MIN_RETRIES`` unless the caller opted out (``0``)."""
    if max_retries > 0 and decision.status in CONGESTION_STATUS_CODES:
        return max(max_retries, CONGESTION_MIN_RETRIES)
    return max_retries


def call_with_retries[T](
    fn: Callable[[float | None], T],
    *,
    max_retries: int,
    timeout: float | None = None,
    deadline: float | None = None,
    event_prefix: str,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    rand: Callable[[], float] | None = None,
) -> T:
    """``fn(attempt_timeout)``, retried on transient errors.

    *fn* gets the HTTP timeout for this attempt: *timeout*, shortened to
    the time left before *deadline* (``None`` when neither is set).  At most
    *max_retries* retries follow the first attempt, except that a 429, 503
    or 529 gets at least ``CONGESTION_MIN_RETRIES`` when *max_retries* > 0
    (``0`` still means no retries).  Waits are :func:`backoff_delay` (base
    2 s, capped at 30 s, raised to a server retry-after up to 65 s); the
    total slept in one call never exceeds ``MAX_TOTAL_WAIT``.  Each retry
    logs ``<event_prefix>rate_limit`` (429) or ``<event_prefix>unavailable``
    (5xx, timeouts, dropped connections) with the effective limit as
    ``max_retries``.  The last exception is re-raised when the error is not
    retryable, the retries are used up, the server's retry-after exceeds
    ``MAX_RETRY_AFTER``, the wait would pass ``MAX_TOTAL_WAIT``, or the next
    attempt would not fit before *deadline*; a :class:`TimeoutError` is
    raised when no attempt fits at all.  *rand* (default
    :func:`random.random`) is the jitter source, injectable for tests.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    rand = rand or random.random
    attempt = 0
    waited = 0.0
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
            if not decision.retryable:
                raise
            limit = _retry_limit(max_retries, decision)
            if attempt >= limit:
                raise
            if decision.retry_after is not None and decision.retry_after > MAX_RETRY_AFTER:
                raise
            wait = backoff_delay(attempt, decision.retry_after, rand=rand)
            if waited + wait > MAX_TOTAL_WAIT:
                raise
            if deadline is not None and clock() + wait + MIN_ATTEMPT_SECONDS > deadline:
                raise
            event = "rate_limit" if decision.kind == KIND_RATE_LIMIT else "unavailable"
            logger.warning(
                f"{event_prefix}{event}",
                attempt=attempt + 1,
                max_retries=limit,
                retry_in=round(wait, 2),
                status=decision.status,
                kind=decision.kind,
                error=str(exc)[:300],
            )
            sleep(wait)
            waited += wait
            attempt += 1
