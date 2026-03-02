"""Tests for retry utilities."""

from __future__ import annotations

import pytest

from framework.retry import retry_sync


def test_retry_sync_succeeds_on_first_try() -> None:
    calls = []

    def fn() -> str:
        calls.append(1)
        return "ok"

    result = retry_sync(fn, max_attempts=3)
    assert result == "ok"
    assert len(calls) == 1


def test_retry_sync_retries_on_failure() -> None:
    calls = []

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
