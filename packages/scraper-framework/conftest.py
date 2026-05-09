"""Root conftest for scraper-framework package.

The historical `collect_ignore_glob` here was a transitional measure for
broken tests that imported `scripts/<name>.py` modules that had since been
moved to `scripts/archive/`.  Those tests have been deleted (issue #4459 —
"scraper-framework tests import archived scripts that no longer exist in
scripts/"), so the glob is no longer needed.

The two `test_reingest_*` tests are still excluded by the CI workflow's
explicit `--ignore=` flags (see `.github/workflows/ci.yml` lines 247-248),
not here — they collect cleanly with the standard scraper-framework venv but
require additional fixtures the CI shard does not stage.
"""

from __future__ import annotations

# No collect_ignore_glob is needed.  Surviving test files in this directory
# all collect cleanly with the scraper-framework `[dev]` venv.


def pytest_configure(config: object) -> None:
    """Patch pytest-xdist DSession to tolerate the worker_workerfinished race.

    pytest-xdist 3.8.0 (and earlier) has a race where ``worker_workerfinished``
    can be called for a node that was already removed from ``_active_nodes``
    during shutdown.  The stock implementation uses ``set.remove()``, which
    raises ``KeyError`` and causes an INTERNALERROR exit code even when all
    tests pass (exit code 3 instead of 0).

    We wrap the method and swallow ``KeyError`` so that the spurious INTERNALERROR
    never reaches the top-level error handler.

    See: https://github.com/pytest-dev/pytest-xdist/issues/1075
    """
    try:
        from xdist.dsession import DSession  # type: ignore[import-untyped]

        _original = DSession.worker_workerfinished

        def _safe_worker_workerfinished(self: object, node: object) -> None:
            try:
                _original(self, node)
            except KeyError:
                # Node already removed from _active_nodes — harmless shutdown race.
                pass

        DSession.worker_workerfinished = _safe_worker_workerfinished  # type: ignore[method-assign]
    except ImportError:
        # pytest-xdist not installed; nothing to patch.
        pass
