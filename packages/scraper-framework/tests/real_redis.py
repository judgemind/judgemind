"""The real-Redis test gate (#5106); the fixture is in conftest.py.

Tests that need a real Redis (Lua scripts, XPENDING / XAUTOCLAIM / XCLAIM
reply shapes, eviction) take the ``real_redis_client`` fixture.  Without
``JUDGEMIND_TEST_REDIS_URL`` they skip, unless ``REQUIRE_REDIS_TESTS=1``
(set in CI, which runs a Redis service), where they FAIL.  A URL that is set
but unreachable also fails under ``REQUIRE_REDIS_TESTS=1``, so a broken CI
service cannot turn the tests into silent skips.  The pre-push hook passes
the local docker-compose Redis (scripts/ensure-test-redis.sh).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

REDIS_URL_ENV = "JUDGEMIND_TEST_REDIS_URL"
REQUIRE_REDIS_ENV = "REQUIRE_REDIS_TESTS"


def connect_real_redis(
    url: str | None,
    required: bool,
    connect: Callable[[str], object],
) -> object:
    """Return a pinged Redis client for ``url``, or skip / fail the test.

    ``connect`` builds and pings a client (raising on failure); it is a
    parameter so the gate itself is unit-tested without a server.
    """
    if not url:
        if required:
            pytest.fail(f"{REQUIRE_REDIS_ENV}=1 but {REDIS_URL_ENV} is not set")
        pytest.skip(f"{REDIS_URL_ENV} not set (real-Redis test)")
    try:
        return connect(url)
    except Exception as exc:  # noqa: BLE001 - any connect/ping error
        msg = f"cannot reach Redis at {url}: {exc!r}"
        if required:
            pytest.fail(f"{REQUIRE_REDIS_ENV}=1 but {msg}")
        pytest.skip(msg)


def connect_and_ping(url: str) -> object:
    """A bytes-reply Redis client for ``url``, pinged once."""
    import redis as redis_lib

    client = redis_lib.Redis.from_url(url, decode_responses=False, socket_connect_timeout=3)
    client.ping()
    return client
