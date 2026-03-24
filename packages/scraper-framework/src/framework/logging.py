"""Shared structlog configuration for all Judgemind entry points.

Provides a single ``configure_structlog()`` function that sets up the canonical
processor chain.  All entry points (ingestion worker, reingest scripts,
cleanup scripts) should call this instead of maintaining their own
``structlog.configure()`` blocks.

Usage::

    from framework.logging import configure_structlog

    configure_structlog()           # JSON in ECS, console locally
    configure_structlog(json=True)  # Always JSON (e.g. ingestion worker)
    configure_structlog(json=True, stdlib_bridge=True)  # Also route stdlib logging

See issues #1806 and #1827 for motivation.
"""

from __future__ import annotations

import logging
import sys

import structlog


def _use_json(json: bool) -> bool:
    """Return True when JSON rendering should be used."""
    return json or not sys.stderr.isatty()


def _shared_tail(*, json: bool) -> list[structlog.types.Processor]:
    """Return the processor tail shared by both the native and stdlib chains.

    Both chains need a timestamper, exception formatter, and renderer.
    Keeping this in one place prevents the two chains from drifting out
    of sync (the exact problem motivating issue #1827).
    """
    tail: list[structlog.types.Processor] = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
    ]
    if _use_json(json):
        tail.append(structlog.processors.JSONRenderer())
    else:
        tail.append(structlog.dev.ConsoleRenderer())
    return tail


def configure_structlog(
    *,
    json: bool = False,
    level: int = logging.INFO,
    contextvars: bool = False,
    stdlib_bridge: bool = False,
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
    stdlib_bridge:
        If ``True``, also configure a ``structlog.stdlib.ProcessorFormatter``
        on the root stdlib logger so that ``logging.getLogger()`` calls are
        routed through structlog.  The stdlib processor chain uses
        ``structlog.stdlib.add_log_level`` and ``ExtraAdder`` (which handle
        stdlib ``LogRecord`` attributes) while sharing the same timestamper,
        exception formatter, and renderer as the native chain.

        Existing ``ProcessorFormatter`` handlers on the root logger are
        removed first to ensure idempotency.

        See issue #1827.
    """
    processors: list[structlog.types.Processor] = []

    if contextvars:
        processors.append(structlog.contextvars.merge_contextvars)

    processors.append(structlog.processors.add_log_level)
    processors.extend(_shared_tail(json=json))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if stdlib_bridge:
        _setup_stdlib_bridge(json=json, level=level)


def _setup_stdlib_bridge(*, json: bool, level: int) -> None:
    """Install a ``ProcessorFormatter`` on the root stdlib logger.

    This routes ``logging.getLogger()`` output through structlog so that
    all log output (native structlog *and* stdlib) uses the same format.

    The processor chain intentionally uses ``structlog.stdlib`` variants
    (``add_log_level``, ``ExtraAdder``) because stdlib ``LogRecord`` objects
    carry level and extra data differently from native structlog events.
    The shared tail (timestamper, exception formatter, renderer) comes from
    ``_shared_tail()`` to prevent drift.
    """
    # Remove any previously-installed ProcessorFormatter handlers to
    # guarantee idempotency when called multiple times.
    for handler in list(logging.root.handlers):
        if isinstance(
            getattr(handler, "formatter", None),
            structlog.stdlib.ProcessorFormatter,
        ):
            logging.root.removeHandler(handler)

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.ExtraAdder(),
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *_shared_tail(json=json),
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.root.addHandler(handler)
    logging.root.setLevel(level)
