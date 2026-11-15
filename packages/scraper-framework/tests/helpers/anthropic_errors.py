"""Real anthropic SDK exceptions, built the way the SDK builds them (#5066).

``anthropic.Anthropic._make_status_error`` picks the exception class from the
HTTP status of an ``httpx2.Response``; :func:`status_error` goes through the
same method, so a 529 is a real ``OverloadedError`` and a 500 a real
``InternalServerError``.  Tests must not invent exception names: a name that
the SDK never raises is what let the old Gemini retry check pass its tests
while retrying nothing (#5056).
"""

from __future__ import annotations

import anthropic
import httpx2

MESSAGES_URL = "https://api.anthropic.com/v1/messages"

_CLIENT = anthropic.Anthropic(api_key="test-key", max_retries=0)


def _request() -> httpx2.Request:
    return httpx2.Request("POST", MESSAGES_URL)


def status_error(status: int, *, headers: dict[str, str] | None = None) -> anthropic.APIStatusError:
    """The exception the SDK raises for an HTTP *status* response."""
    body = {"type": "error", "error": {"type": "api_error", "message": f"HTTP {status}"}}
    response = httpx2.Response(status, headers=headers or {}, json=body, request=_request())
    return _CLIENT._make_status_error(f"Error code: {status}", body=body, response=response)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=_request())


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_request())
