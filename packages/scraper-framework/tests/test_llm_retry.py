"""Tests for the shared LLM retry policy (ingestion/llm_retry.py, #5056).

Every exception here is the real type the SDKs raise: google-genai's
``ClientError`` / ``ServerError`` (built the way the SDK builds them) and
httpx's transport errors.  Made-up exception names are what let the old
class-name check pass its tests while retrying nothing in production.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from google.genai import errors as genai_errors
from helpers.anthropic_errors import connection_error, status_error, timeout_error

from ingestion import llm_retry
from ingestion.llm_retry import (
    KIND_CLIENT_ERROR,
    KIND_CONNECTION,
    KIND_OTHER,
    KIND_RATE_LIMIT,
    KIND_SERVER_ERROR,
    KIND_TIMEOUT,
    MAX_DELAY,
    backoff_delay,
    call_with_retries,
    classify_error,
)


def _genai_error(
    code: int, *, details: list | None = None, headers: dict | None = None
) -> genai_errors.APIError:
    body: dict = {"error": {"code": code, "message": "m", "status": "S"}}
    if details is not None:
        body["error"]["details"] = details
    response = httpx.Response(code, headers=headers or {}, json=body)
    try:
        genai_errors.APIError.raise_error(code, body, response)
    except genai_errors.APIError as exc:
        return exc
    raise AssertionError("unreachable")  # pragma: no cover


class FakeClock:
    """A monotonic clock that only moves when ``sleep`` is called."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestClassifyError:
    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_server_errors_retryable(self, code: int) -> None:
        exc = _genai_error(code)
        assert isinstance(exc, genai_errors.ServerError)
        decision = classify_error(exc)
        assert decision.retryable
        assert decision.kind == KIND_SERVER_ERROR
        assert decision.status == code

    def test_429_retryable_rate_limit(self) -> None:
        exc = _genai_error(429)
        assert isinstance(exc, genai_errors.ClientError)
        decision = classify_error(exc)
        assert decision.retryable
        assert decision.kind == KIND_RATE_LIMIT

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    def test_client_errors_not_retryable(self, code: int) -> None:
        decision = classify_error(_genai_error(code))
        assert not decision.retryable
        assert decision.kind == KIND_CLIENT_ERROR
        assert decision.status == code

    def test_501_not_retryable(self) -> None:
        decision = classify_error(_genai_error(501))
        assert not decision.retryable
        assert decision.kind == KIND_OTHER

    @pytest.mark.parametrize(
        "exc",
        [httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), TimeoutError("t")],
    )
    def test_timeouts_retryable(self, exc: Exception) -> None:
        decision = classify_error(exc)
        assert decision.retryable
        assert decision.kind == KIND_TIMEOUT

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("refused"),
            httpx.ReadError("reset"),
            httpx.RemoteProtocolError("Server disconnected"),
            ConnectionResetError(104, "Connection reset by peer"),
        ],
    )
    def test_connection_errors_retryable(self, exc: Exception) -> None:
        decision = classify_error(exc)
        assert decision.retryable
        assert decision.kind == KIND_CONNECTION

    def test_old_name_matching_is_gone(self) -> None:
        """A class merely *named* like an api_core error is not retried."""

        class ServiceUnavailable(Exception):  # noqa: N818 — the old api_core name
            pass

        assert not classify_error(ServiceUnavailable("503")).retryable
        assert not classify_error(RuntimeError("boom")).retryable

    def test_retry_after_header(self) -> None:
        exc = _genai_error(429, headers={"Retry-After": "12"})
        assert classify_error(exc).retry_after == 12.0

    def test_retry_info_delay(self) -> None:
        info = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2.5s"}
        exc = _genai_error(429, details=[info])
        assert classify_error(exc).retry_after == 2.5

    def test_no_retry_after(self) -> None:
        assert classify_error(_genai_error(503)).retry_after is None

    def test_unparseable_retry_after_ignored(self) -> None:
        exc = _genai_error(503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        assert classify_error(exc).retry_after is None

    def test_error_without_response(self) -> None:
        exc = genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
        decision = classify_error(exc)
        assert decision.retryable
        assert decision.retry_after is None


class TestClassifyAnthropicError:
    """The real anthropic SDK exceptions, built by the SDK's own
    ``_make_status_error`` (#5066)."""

    @pytest.mark.parametrize(
        ("code", "expected_type"),
        [
            (500, anthropic.InternalServerError),
            (502, anthropic.InternalServerError),
            (503, anthropic.InternalServerError),
            (504, anthropic.InternalServerError),
            (529, anthropic.OverloadedError),
        ],
    )
    def test_anthropic_server_errors_retryable(self, code: int, expected_type: type) -> None:
        exc = status_error(code)
        assert type(exc) is expected_type
        decision = classify_error(exc)
        assert decision.retryable
        assert decision.kind == KIND_SERVER_ERROR
        assert decision.status == code

    def test_anthropic_429_retryable_rate_limit(self) -> None:
        exc = status_error(429)
        assert type(exc) is anthropic.RateLimitError
        decision = classify_error(exc)
        assert decision.retryable
        assert decision.kind == KIND_RATE_LIMIT
        assert decision.status == 429

    @pytest.mark.parametrize(
        ("code", "expected_type"),
        [
            (400, anthropic.BadRequestError),
            (401, anthropic.AuthenticationError),
            (403, anthropic.PermissionDeniedError),
            (404, anthropic.NotFoundError),
            (413, anthropic.RequestTooLargeError),
        ],
    )
    def test_anthropic_client_errors_not_retryable(self, code: int, expected_type: type) -> None:
        exc = status_error(code)
        assert type(exc) is expected_type
        decision = classify_error(exc)
        assert not decision.retryable
        assert decision.kind == KIND_CLIENT_ERROR
        assert decision.status == code

    def test_anthropic_timeout_retryable(self) -> None:
        decision = classify_error(timeout_error())
        assert decision.retryable
        assert decision.kind == KIND_TIMEOUT

    def test_anthropic_connection_error_retryable(self) -> None:
        decision = classify_error(connection_error())
        assert decision.retryable
        assert decision.kind == KIND_CONNECTION

    def test_anthropic_retry_after_header(self) -> None:
        assert classify_error(status_error(429, headers={"retry-after": "4"})).retry_after == 4.0

    def test_anthropic_retry_after_ms_header_wins(self) -> None:
        exc = status_error(529, headers={"retry-after-ms": "1500", "retry-after": "9"})
        assert classify_error(exc).retry_after == 1.5

    def test_anthropic_no_retry_after(self) -> None:
        assert classify_error(status_error(529)).retry_after is None

    def test_anthropic_base_api_error_not_retryable(self) -> None:
        """A bare ``APIError`` (no HTTP status, not a transport failure)."""
        exc = anthropic.APIError("m", request=timeout_error().request, body=None)
        assert not classify_error(exc).retryable

    def test_anthropic_529_retried_through_call_with_retries(self) -> None:
        clock = FakeClock()
        calls: list[float | None] = []

        def fn(attempt_timeout: float | None) -> str:
            calls.append(attempt_timeout)
            if len(calls) < 3:
                raise status_error(529 if len(calls) == 1 else 429)
            return "ok"

        result = call_with_retries(
            fn, max_retries=2, event_prefix="t_", sleep=clock.sleep, clock=clock
        )

        assert result == "ok"
        assert len(calls) == 3
        assert len(clock.sleeps) == 2

    def test_anthropic_400_not_retried_through_call_with_retries(self) -> None:
        clock = FakeClock()
        fn = MagicMock(side_effect=status_error(400))

        with pytest.raises(anthropic.BadRequestError):
            call_with_retries(fn, max_retries=3, event_prefix="t_", sleep=clock.sleep, clock=clock)

        assert fn.call_count == 1
        assert clock.sleeps == []

    def test_anthropic_deadline_respected(self) -> None:
        """A 529 retry that would end past the deadline is not made."""
        clock = FakeClock(now=100.0)
        fn = MagicMock(side_effect=status_error(529))

        with pytest.raises(anthropic.OverloadedError):
            call_with_retries(
                fn,
                max_retries=5,
                deadline=101.2,
                event_prefix="t_",
                sleep=clock.sleep,
                clock=clock,
            )

        assert fn.call_count == 1
        assert clock.sleeps == []


class TestBackoffDelay:
    def test_exponential_with_jitter_bounds(self) -> None:
        for attempt, step in [(0, 1.0), (1, 2.0), (2, 4.0)]:
            assert backoff_delay(attempt, rand=lambda: 0.0) == step / 2
            assert backoff_delay(attempt, rand=lambda: 1.0) == step

    def test_retry_after_raises_the_wait(self) -> None:
        assert backoff_delay(0, 9.0, rand=lambda: 0.0) == 9.0

    def test_capped(self) -> None:
        assert backoff_delay(20, rand=lambda: 1.0) == MAX_DELAY


class TestCallWithRetries:
    def test_success_first_try(self) -> None:
        fn = MagicMock(return_value="ok")
        assert call_with_retries(fn, max_retries=2, event_prefix="t.") == "ok"
        fn.assert_called_once_with(None)

    def test_retries_then_succeeds(self) -> None:
        clock = FakeClock()
        fn = MagicMock(side_effect=[_genai_error(503), _genai_error(504), "ok"])
        result = call_with_retries(
            fn, max_retries=2, event_prefix="t.", sleep=clock.sleep, clock=clock
        )
        assert result == "ok"
        assert fn.call_count == 3
        assert len(clock.sleeps) == 2

    def test_400_raised_without_retry(self) -> None:
        clock = FakeClock()
        exc = _genai_error(400)
        fn = MagicMock(side_effect=exc)
        with pytest.raises(genai_errors.ClientError):
            call_with_retries(fn, max_retries=3, event_prefix="t.", sleep=clock.sleep, clock=clock)
        assert fn.call_count == 1
        assert clock.sleeps == []

    def test_exhausted_reraises_last(self) -> None:
        clock = FakeClock()
        fn = MagicMock(side_effect=_genai_error(503))
        with pytest.raises(genai_errors.ServerError):
            call_with_retries(fn, max_retries=2, event_prefix="t.", sleep=clock.sleep, clock=clock)
        assert fn.call_count == 3

    def test_deadline_bounds_total_time(self) -> None:
        """Retries stop once the next wait plus an attempt would pass the deadline."""
        clock = FakeClock(now=0.0)
        fn = MagicMock(side_effect=_genai_error(503))
        with pytest.raises(genai_errors.ServerError):
            call_with_retries(
                fn,
                max_retries=10,
                deadline=5.0,
                event_prefix="t.",
                sleep=clock.sleep,
                clock=clock,
            )
        assert clock.now <= 5.0
        assert fn.call_count < 11

    def test_attempt_timeout_shortened_to_deadline(self) -> None:
        clock = FakeClock(now=0.0)
        fn = MagicMock(return_value="ok")
        call_with_retries(
            fn, max_retries=1, timeout=30.0, deadline=4.0, event_prefix="t.", clock=clock
        )
        fn.assert_called_once_with(4.0)

    def test_attempt_timeout_from_deadline_alone(self) -> None:
        clock = FakeClock(now=0.0)
        fn = MagicMock(return_value="ok")
        call_with_retries(fn, max_retries=1, deadline=7.0, event_prefix="t.", clock=clock)
        fn.assert_called_once_with(7.0)

    def test_no_attempt_after_deadline(self) -> None:
        clock = FakeClock(now=10.0)
        fn = MagicMock()
        with pytest.raises(TimeoutError):
            call_with_retries(fn, max_retries=2, deadline=10.5, event_prefix="t.", clock=clock)
        fn.assert_not_called()

    def test_long_retry_after_not_waited(self) -> None:
        """A server asking for more than MAX_DELAY is a quota window: fail now."""
        clock = FakeClock()
        exc = _genai_error(429, headers={"Retry-After": str(int(MAX_DELAY) + 30)})
        fn = MagicMock(side_effect=exc)
        with pytest.raises(genai_errors.ClientError):
            call_with_retries(fn, max_retries=3, event_prefix="t.", sleep=clock.sleep, clock=clock)
        assert fn.call_count == 1

    def test_retry_log_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logger = MagicMock()
        monkeypatch.setattr(llm_retry, "logger", logger)
        clock = FakeClock()
        fn = MagicMock(side_effect=[_genai_error(429), httpx.ReadTimeout("t"), "ok"])
        call_with_retries(fn, max_retries=2, event_prefix="p.", sleep=clock.sleep, clock=clock)
        events = [c.args[0] for c in logger.warning.call_args_list]
        assert events == ["p.rate_limit", "p.unavailable"]
        assert logger.warning.call_args_list[0].kwargs["status"] == 429
        assert logger.warning.call_args_list[1].kwargs["kind"] == KIND_TIMEOUT

    def test_default_sleep_and_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(llm_retry.time, "sleep", sleeps.append)
        fn = MagicMock(side_effect=[_genai_error(500), "ok"])
        assert call_with_retries(fn, max_retries=1, event_prefix="t.") == "ok"
        assert len(sleeps) == 1
