"""Tests for retry utilities."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from framework.retry import retry_async, retry_sync

# ── retry_sync tests ──


def test_retry_sync_succeeds_on_first_try() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "ok"

    result = retry_sync(fn, max_attempts=3)
    assert result == "ok"
    assert len(calls) == 1


def test_retry_sync_retries_on_failure() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("transient")
        return "ok"

    result = retry_sync(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert len(calls) == 3


def test_retry_sync_raises_after_exhaustion() -> None:
    def fn() -> None:
        raise RuntimeError("always fails")

    with pytest.raises(RuntimeError, match="always fails"):
        retry_sync(fn, max_attempts=3, base_delay=0)


def test_retry_sync_only_catches_specified_exceptions() -> None:
    def fn() -> None:
        raise TypeError("unexpected type")

    with pytest.raises(TypeError):
        retry_sync(fn, max_attempts=3, base_delay=0, exceptions=(ValueError,))


def test_retry_sync_backoff_timing() -> None:
    """Verify exponential backoff doubles the delay on each retry."""
    sleep_calls: list[float] = []

    def fn() -> None:
        raise ValueError("fail")

    with (
        patch("framework.retry.time.sleep", side_effect=lambda t: sleep_calls.append(t)),
        pytest.raises(ValueError, match="fail"),
    ):
        retry_sync(fn, max_attempts=4, base_delay=1.0, max_delay=60.0)

    # 3 sleeps between 4 attempts: 1.0, 2.0, 4.0
    assert sleep_calls == [1.0, 2.0, 4.0]


def test_retry_sync_max_delay_cap() -> None:
    """Verify delay is capped at max_delay."""
    sleep_calls: list[float] = []

    def fn() -> None:
        raise ValueError("fail")

    with (
        patch("framework.retry.time.sleep", side_effect=lambda t: sleep_calls.append(t)),
        pytest.raises(ValueError, match="fail"),
    ):
        retry_sync(fn, max_attempts=5, base_delay=2.0, max_delay=5.0)

    # Delays: 2.0, 4.0, 5.0 (capped), 5.0 (capped)
    assert sleep_calls == [2.0, 4.0, 5.0, 5.0]


def test_retry_sync_single_attempt() -> None:
    """With max_attempts=1, the function is called once and no retry occurs."""

    def fn() -> None:
        raise ValueError("fail")

    with pytest.raises(ValueError, match="fail"):
        retry_sync(fn, max_attempts=1, base_delay=0)


# ── retry_async tests ──


@pytest.mark.asyncio
async def test_retry_async_succeeds_on_first_try() -> None:
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        return "ok"

    result = await retry_async(fn, max_attempts=3)
    assert result == "ok"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_async_retries_on_failure() -> None:
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("transient")
        return "ok"

    result = await retry_async(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_retry_async_raises_after_exhaustion() -> None:
    async def fn() -> None:
        raise RuntimeError("always fails")

    with pytest.raises(RuntimeError, match="always fails"):
        await retry_async(fn, max_attempts=3, base_delay=0)


@pytest.mark.asyncio
async def test_retry_async_only_catches_specified_exceptions() -> None:
    async def fn() -> None:
        raise TypeError("unexpected type")

    with pytest.raises(TypeError):
        await retry_async(fn, max_attempts=3, base_delay=0, exceptions=(ValueError,))


@pytest.mark.asyncio
async def test_retry_async_backoff_timing() -> None:
    """Verify async exponential backoff doubles the delay on each retry."""
    sleep_calls: list[float] = []

    async def fn() -> None:
        raise ValueError("fail")

    async def mock_sleep(t: float) -> None:
        sleep_calls.append(t)

    with (
        patch("framework.retry.asyncio.sleep", side_effect=mock_sleep),
        pytest.raises(ValueError, match="fail"),
    ):
        await retry_async(fn, max_attempts=4, base_delay=1.0, max_delay=60.0)

    assert sleep_calls == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_retry_async_max_delay_cap() -> None:
    """Verify async delay is capped at max_delay."""
    sleep_calls: list[float] = []

    async def fn() -> None:
        raise ValueError("fail")

    async def mock_sleep(t: float) -> None:
        sleep_calls.append(t)

    with (
        patch("framework.retry.asyncio.sleep", side_effect=mock_sleep),
        pytest.raises(ValueError, match="fail"),
    ):
        await retry_async(fn, max_attempts=5, base_delay=2.0, max_delay=5.0)

    assert sleep_calls == [2.0, 4.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_retry_async_single_attempt() -> None:
    """With max_attempts=1, the function is called once and no retry occurs."""

    async def fn() -> None:
        raise ValueError("fail")

    with pytest.raises(ValueError, match="fail"):
        await retry_async(fn, max_attempts=1, base_delay=0)
