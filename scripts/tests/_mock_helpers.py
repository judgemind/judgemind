"""Shared helpers for ``scripts/tests/test_*.py`` (#4430).

The lightweight CI ``scripts-tests (python)`` shard installs only
``pytest pytest-xdist boto3 -e packages/judgemind-config`` (see
``scripts/tests/README.md``). Test files whose script-under-test imports
heavyweight packages at module load (``psycopg``, ``structlog``,
``framework.*``, ``ingestion.*``) must inject ``MagicMock`` entries into
``sys.modules`` BEFORE the import statement runs, then restore the prior
``sys.modules`` state after the import — otherwise the mocks leak across
test files in the same pytest process and break later tests that import the
real modules (#4426).

Before #4430 every test file maintained its own save/restore boilerplate
around the import. The boilerplate is fragile: a future test author
following only the README's "Pattern" example, which does not show the
restore loop, leaks mocks into ``sys.modules`` and breaks unrelated tests.

This module centralises the pattern in one place via the ``mock_sys_modules``
context manager, so:

* ``__enter__`` snapshots ``sys.modules`` for every named module and
  installs a ``MagicMock`` (or the caller-supplied mock) for each one.
* ``__exit__`` restores the snapshot — even if the import inside the
  ``with`` block raised — so no mock survives outside the context.

Typical usage at the top of a test file::

    from tests._mock_helpers import mock_sys_modules

    with mock_sys_modules(["structlog", "framework", "framework.logging"]):
        import script_under_test as _script  # noqa: E402

When the script-under-test calls ``structlog.get_logger()`` at module load
(common via ``framework.logging.configure_structlog``), pre-seed the
``get_logger`` attribute on a caller-built mock and pass the mapping form::

    _mock_structlog = MagicMock()
    _mock_structlog.get_logger.return_value = MagicMock()
    with mock_sys_modules({
        "structlog": _mock_structlog,
        "framework": MagicMock(),
        "framework.logging": MagicMock(),
    }):
        import script_under_test as _script  # noqa: E402

Compatibility: the helper is purely additive. Existing files that already
restore ``sys.modules`` correctly continue to work unchanged. The
``test_scripts_tests_isolation.py`` regression (#4426) still pins the
no-leak invariant — every migrated file passes that probe by construction
because the context manager's ``__exit__`` restores on every exit path,
including exception propagation.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping
from unittest.mock import MagicMock

__all__ = ["mock_sys_modules"]


@contextmanager
def mock_sys_modules(
    modules: Iterable[str] | Mapping[str, Any],
) -> Iterator[dict[str, Any]]:
    """Install ``MagicMock`` entries in ``sys.modules`` for ``modules`` and
    restore the prior state on exit.

    Args:
        modules: Either an iterable of module names (a fresh ``MagicMock``
            is built for each) or a mapping of ``name -> mock`` for cases
            where attributes must be pre-seeded on individual mocks (e.g.
            ``structlog.get_logger.return_value``).

    Yields:
        A dict mapping each module name to the mock that was installed.
        Callers can mutate the returned mocks after entering the context
        if they need to set attributes for the upcoming import.

    The ``__exit__`` path restores ``sys.modules`` to its pre-context
    state for every named module — pre-existing entries are reinstalled,
    and entries that did not exist before the context are deleted. This
    guarantee holds even if the body of the ``with`` block raises.
    """
    # Defer the ``sys`` import to call-time so the helper survives any
    # ``sys.modules`` rebinding shenanigans during test collection.
    import sys

    if isinstance(modules, Mapping):
        installs: dict[str, Any] = dict(modules)
    else:
        installs = {name: MagicMock() for name in modules}

    saved: dict[str, Any] = {}
    had_key: dict[str, bool] = {}
    for name in installs:
        had_key[name] = name in sys.modules
        if had_key[name]:
            saved[name] = sys.modules[name]

    for name, mock in installs.items():
        sys.modules[name] = mock

    try:
        yield installs
    finally:
        # Restore in reverse-insertion order so nested usage (an outer
        # ``with`` plus an inner ``with`` reusing one of the names) sees
        # the correct intermediate state on each exit.
        for name in reversed(list(installs.keys())):
            if had_key[name]:
                sys.modules[name] = saved[name]
            elif name in sys.modules:
                del sys.modules[name]
