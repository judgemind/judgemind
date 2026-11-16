"""Regression tests for the structlog test-isolation block in conftest (#5113).

``configure_structlog()`` turns on ``cache_logger_on_first_use``. A module
logger cached under one processor list kept writing to it after a later
``structlog.configure()`` installed a new list, so ``capture_logs()`` missed
its events and LLM tests failed or passed depending on test order.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
import structlog

from framework.logging import configure_structlog


@pytest.fixture
def _restore_structlog_config() -> Generator[None, None, None]:
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.mark.usefixtures("_restore_structlog_config")
def test_configure_cannot_turn_logger_caching_on() -> None:
    structlog.configure(cache_logger_on_first_use=True)
    assert structlog.get_config()["cache_logger_on_first_use"] is False


@pytest.mark.usefixtures("_restore_structlog_config")
def test_capture_logs_sees_logger_used_before_a_reconfigure() -> None:
    """The #5113 sequence: configure, use a module logger, reconfigure, capture."""
    configure_structlog(json=True)  # asks for caching, as import-time calls do
    logger = structlog.get_logger("test_structlog_test_isolation")
    logger.info("first_use")  # would freeze the logger's processor list
    configure_structlog(json=True)  # installs a new processor list

    with structlog.testing.capture_logs() as logs:
        logger.warning("after_reconfigure", status_code=529)

    assert [e["event"] for e in logs] == ["after_reconfigure"]
    assert logs[0]["status_code"] == 529
