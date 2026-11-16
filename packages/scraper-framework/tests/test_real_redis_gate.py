"""The real-Redis test gate (#5106).

Real-Redis tests take the ``real_redis_client`` fixture from conftest.py.
CI runs a Redis service and sets ``REQUIRE_REDIS_TESTS=1``, so a missing or
unreachable Redis must FAIL those tests there rather than skip them.  Locally
(no ``REQUIRE_REDIS_TESTS``) they skip.
"""

from __future__ import annotations

import os

import pytest
from _pytest.outcomes import Failed, Skipped
from real_redis import REDIS_URL_ENV, REQUIRE_REDIS_ENV, connect_real_redis


def _refuse(url: str) -> object:
    raise ConnectionError(f"refused {url}")


def test_redis_is_configured_when_required() -> None:
    """CI sets ``REQUIRE_REDIS_TESTS=1``: the URL must be set there."""
    if os.environ.get(REQUIRE_REDIS_ENV) == "1" and not os.environ.get(REDIS_URL_ENV):
        pytest.fail(f"{REQUIRE_REDIS_ENV}=1 but {REDIS_URL_ENV} is not set")


def test_no_url_skips_when_not_required() -> None:
    with pytest.raises(Skipped, match="not set"):
        connect_real_redis(None, False, _refuse)


def test_no_url_fails_when_required() -> None:
    with pytest.raises(
        Failed, match="REQUIRE_REDIS_TESTS=1 but JUDGEMIND_TEST_REDIS_URL is not set"
    ):
        connect_real_redis("", True, _refuse)


def test_unreachable_url_skips_when_not_required() -> None:
    with pytest.raises(Skipped, match="cannot reach Redis at redis://nowhere:1/0"):
        connect_real_redis("redis://nowhere:1/0", False, _refuse)


def test_unreachable_url_fails_when_required() -> None:
    with pytest.raises(Failed, match="REQUIRE_REDIS_TESTS=1 but cannot reach Redis"):
        connect_real_redis("redis://nowhere:1/0", True, _refuse)


def test_reachable_url_returns_the_client() -> None:
    client = object()
    assert connect_real_redis("redis://here:6379/0", True, lambda _url: client) is client


def test_fixture_runs_against_a_real_redis(real_redis_client: object) -> None:
    """Under REQUIRE_REDIS_TESTS=1 this proves the fixture reached a server."""
    key = f"test-5106-{os.getpid()}-{id(real_redis_client)}"
    real_redis_client.set(key, b"1", ex=30)  # type: ignore[attr-defined]
    try:
        assert real_redis_client.get(key) == b"1"  # type: ignore[attr-defined]
    finally:
        real_redis_client.delete(key)  # type: ignore[attr-defined]
