"""Shared structlog configuration for all Judgemind entry points.

Provides a single ``configure_structlog()`` function that sets up the canonical
processor chain.  All entry points (ingestion worker, reingest scripts,
cleanup scripts) should call this instead of maintaining their own
``structlog.configure()`` blocks.

Usage::

    from framework.logging import configure_structlog

    configure_structlog()           # JSON in ECS, console locally
    configure_structlog(json=True)  # Always JSON (e.g. ingestion worker)

See issue #1806 for motivation.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_structlog(
    *,
    json: bool = False,
    level: int = logging.INFO,
    contextvars: bool = False,
) -> None:
    """Configure structlog with the standard Judgemind processor chain.

    Parameters
    ----------
    json:
        If ``True``, always use ``JSONRenderer`` regardless of terminal.
        If ``False`` (default), use ``ConsoleRenderer`` when stderr is a TTY
        and ``JSONRenderer`` otherwise.
    level:
        Minimum log level (default ``logging.INFO``).
    contextvars:
        If ``True``, prepend ``structlog.contextvars.merge_contextvars``
        to the processor chain.  Useful for scripts that bind context
        variables across function boundaries.
    """
    processors: list[structlog.types.Processor] = []

    if contextvars:
        processors.append(structlog.contextvars.merge_contextvars)

    processors.extend(
        [
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
        ]
    )

    if json or not sys.stderr.isatty():
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
