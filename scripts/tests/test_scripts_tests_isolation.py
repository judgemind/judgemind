# venv: scraper-framework
"""Cross-file isolation regression for scripts/tests (#4426).

Many ``scripts/tests/test_*.py`` files inject ``MagicMock`` modules into
``sys.modules`` to make their script-under-test importable in the lightweight
CI ``scripts-tests (python)`` shard (which only installs pytest, pytest-xdist,
boto3, judgemind-config — no ``structlog`` / ``framework`` / ``ingestion``).

The pattern is

    sys.modules["structlog"] = MagicMock()
    import script_under_test  # noqa: E402

If the file then leaves the ``MagicMock`` in ``sys.modules`` after the import,
every later test in the same pytest process that touches the real
``structlog`` (e.g.
``test_drain_splitter_carry_forward_clusters.py::TestLoggerExtraFieldsSurfaceInOutput``,
which calls ``configure_structlog`` from real ``framework.logging`` and runs
``isinstance(..., structlog.stdlib.ProcessorFormatter)``) blows up with
``TypeError: isinstance() arg 2 must be a type, a tuple of types, or a
union`` because ``structlog.stdlib.ProcessorFormatter`` is now a ``MagicMock``
instead of a class.

This regression test pins the cross-file isolation invariant: importing every
``scripts/tests/test_*.py`` file in alphabetical order (the same order pytest
collects in) must NOT leave ``structlog`` / ``framework`` / ``framework.logging``
replaced by a ``MagicMock`` after the imports complete. The same invariant
extends to any other ``MagicMock``-pollution-class real module the
``scripts-tests`` shard may import later in the run.

If this test starts failing, a new (or modified) ``scripts/tests/test_*.py``
forgot to restore ``sys.modules`` after its module-level import-side-effect
block. The fix is the same restore pattern that
``test_audit_correctly_labeled_s3_orphans.py`` and
``test_audit_llm_carry_forward.py`` use:

    _saved_modules: dict[str, object] = {}
    for _mod_name, _mock_mod in _modules_to_mock.items():
        if _mod_name in sys.modules:
            _saved_modules[_mod_name] = sys.modules[_mod_name]
        sys.modules[_mod_name] = _mock_mod

    import script_under_test  # noqa: E402

    # Restore so other test files see real modules.
    for _mod_name in list(_modules_to_mock.keys()):
        if _mod_name in _saved_modules:
            sys.modules[_mod_name] = _saved_modules[_mod_name]
        elif _mod_name in sys.modules:
            del sys.modules[_mod_name]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent


def test_no_test_file_leaves_structlog_mocked_in_sys_modules() -> None:
    """Subprocess probe: import every ``scripts/tests/test_*.py`` in
    alphabetical order, then verify ``sys.modules['structlog']`` and
    ``sys.modules['framework.logging']`` (when present) are NOT
    ``MagicMock`` instances.

    Runs in a subprocess so the test's own ``sys.modules`` state doesn't
    interfere with whatever the test files install. The subprocess imports
    real ``structlog`` first (when available) so the "before" state is
    deterministic, then walks the alphabetised file list. The probe is
    skipped on environments where ``structlog`` is not installed at all
    (matching the lightweight CI ``scripts-tests (python)`` shard, which
    runs each test in pytest-xdist isolation and so doesn't hit this
    cross-file class of bug).
    """
    pytest.importorskip("structlog")

    test_files = sorted(
        f.name
        for f in _TESTS_DIR.iterdir()
        if f.name.startswith("test_")
        and f.name.endswith(".py")
        and f.name != Path(__file__).name
    )

    probe_script = (
        "import sys, importlib\n"
        f"sys.path.insert(0, {str(_TESTS_DIR)!r})\n"
        "from unittest.mock import MagicMock\n"
        "import structlog  # establish real structlog binding before any imports\n"
        "real_structlog = structlog\n"
        "polluters = []\n"
        "for fname in " + repr(test_files) + ":\n"
        "    mod_name = fname[:-3]\n"
        "    try:\n"
        "        importlib.import_module(mod_name)\n"
        "    except Exception:\n"
        "        # An import failure is a different bug — out of scope here.\n"
        "        continue\n"
        "    sl = sys.modules.get('structlog')\n"
        "    fl = sys.modules.get('framework.logging')\n"
        "    fr = sys.modules.get('framework')\n"
        "    if isinstance(sl, MagicMock) or isinstance(fl, MagicMock) or isinstance(fr, MagicMock):\n"
        "        polluters.append((fname, type(sl).__name__, type(fl).__name__ if fl is not None else 'absent', type(fr).__name__ if fr is not None else 'absent'))\n"
        "        break  # first polluter is enough — report and stop\n"
        "if polluters:\n"
        "    print('POLLUTED:' + repr(polluters))\n"
        "else:\n"
        "    print('CLEAN')\n"
    )

    probe_path = _TESTS_DIR / "_probe_isolation_subprocess.py"
    probe_path.write_text(probe_script, encoding="utf-8")
    try:
        venv_python = sys.executable
        # Run the subprocess from the repo root so relative paths in the
        # test files resolve the same way they do under pytest.
        repo_root = _TESTS_DIR.parent.parent
        env = os.environ.copy()
        # Make sure the subprocess uses the same venv (its sys.path is
        # already correct because we use ``sys.executable``).
        result = subprocess.run(
            [venv_python, str(probe_path)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo_root),
            env=env,
        )
    finally:
        probe_path.unlink(missing_ok=True)

    assert result.returncode == 0, (
        f"isolation probe subprocess crashed: rc={result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    output = result.stdout.strip()
    assert output.startswith("CLEAN"), (
        f"sys.modules pollution detected — a scripts/tests test_*.py file "
        f"left a MagicMock in sys.modules after its module-level import. "
        f"Probe output: {output!r}\n"
        f"stderr: {result.stderr!r}\n"
        f"Fix: in the offending file, after ``import script_under_test``, "
        f"restore sys.modules to its saved state. See "
        f"scripts/tests/test_audit_correctly_labeled_s3_orphans.py for the "
        f"canonical pattern."
    )


def test_real_structlog_stdlib_processor_formatter_is_a_class() -> None:
    """Sanity: in this test process, ``structlog.stdlib.ProcessorFormatter``
    must be a class (not a ``MagicMock``).

    This test runs in the SAME pytest process as every other
    ``scripts/tests/test_*.py`` file, so if any of them pollutes
    ``sys.modules['structlog']`` and runs first (alphabetical-collection
    order), this test catches the pollution at the layer the bug lives.

    The test is alphabetically near the end (``test_scripts_tests_isolation``
    sorts after ``test_s*``) so it runs after most candidate polluters in
    the default collection order.
    """
    pytest.importorskip("structlog")
    import structlog

    # When structlog is the real module, ``structlog.stdlib.ProcessorFormatter``
    # is a class derived from ``logging.Formatter``. When a prior test
    # polluted ``sys.modules``, both ``structlog.stdlib`` and
    # ``ProcessorFormatter`` are ``MagicMock`` instances and the
    # ``isinstance`` check below crashes with ``TypeError`` instead of
    # returning ``False``.
    formatter_cls = structlog.stdlib.ProcessorFormatter
    assert isinstance(formatter_cls, type), (
        f"structlog.stdlib.ProcessorFormatter must be a class, "
        f"got {type(formatter_cls).__name__}: {formatter_cls!r}. "
        f"This means an earlier scripts/tests test_*.py file polluted "
        f"sys.modules['structlog'] with a MagicMock and didn't restore. "
        f"See test_no_test_file_leaves_structlog_mocked_in_sys_modules "
        f"in this file for the cross-file probe."
    )

    # Sanity: isinstance(..., formatter_cls) must NOT crash with TypeError.
    # If structlog is polluted, this is the line in
    # framework/logging.py:_setup_stdlib_bridge that fails (#4368).
    try:
        isinstance(None, formatter_cls)
    except TypeError as e:
        pytest.fail(
            f"isinstance(..., structlog.stdlib.ProcessorFormatter) raised "
            f"TypeError: {e}. structlog is polluted in sys.modules — see "
            f"test_no_test_file_leaves_structlog_mocked_in_sys_modules."
        )
