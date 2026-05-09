"""Tests for ``scripts/tests/_mock_helpers.py`` (#4430).

The helper centralises the module-level ``sys.modules`` save/replay
pattern that ~10 ``scripts/tests/test_*.py`` files used to maintain by
hand. These tests pin the contract that issue #4430 calls out:

* Existing files that already restore correctly keep working unchanged.
* New files that use the helper get the restore for free, even if their
  import-under-test raises.
* The helper accepts both an iterable-of-names and a dict shape so
  pre-seeded mocks (e.g. ``structlog.get_logger.return_value``) compose
  cleanly.

These run in the same lightweight pytest environment the other
``scripts/tests/test_*.py`` files do (no ``structlog`` / ``framework``
required) — the helper's behaviour is pure ``sys.modules`` plumbing, so
the tests use ``"_mock_helpers_probe_<n>"`` style sentinel module names
rather than real package names to avoid collateral damage to the wider
pytest session.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from tests._mock_helpers import mock_sys_modules


class TestIterableForm:
    """``mock_sys_modules`` with an iterable of names builds fresh mocks."""

    def test_each_name_gets_a_magicmock(self) -> None:
        names = ["_mock_helpers_probe_a", "_mock_helpers_probe_b"]
        for name in names:
            assert name not in sys.modules

        with mock_sys_modules(names) as installed:
            for name in names:
                assert sys.modules[name] is installed[name]
                assert isinstance(installed[name], MagicMock)

        for name in names:
            assert name not in sys.modules

    def test_yielded_dict_keys_match_input_iterable(self) -> None:
        names = ["_mock_helpers_probe_c", "_mock_helpers_probe_d"]
        with mock_sys_modules(names) as installed:
            assert set(installed.keys()) == set(names)


class TestMappingForm:
    """``mock_sys_modules`` with a mapping installs the caller's mocks verbatim."""

    def test_caller_supplied_mock_is_installed(self) -> None:
        sentinel = MagicMock()
        sentinel.get_logger.return_value = MagicMock()
        with mock_sys_modules({"_mock_helpers_probe_e": sentinel}) as installed:
            assert sys.modules["_mock_helpers_probe_e"] is sentinel
            assert installed["_mock_helpers_probe_e"] is sentinel
        assert "_mock_helpers_probe_e" not in sys.modules

    def test_pre_seeded_attribute_survives_into_with_block(self) -> None:
        """Attributes set on the mock BEFORE the context manager is entered
        must be visible inside the body — otherwise the ``configure_structlog``
        callsite at script-under-test module load fails.
        """
        sentinel = MagicMock()
        marker = object()
        sentinel.configure_structlog.return_value = marker
        with mock_sys_modules({"_mock_helpers_probe_f": sentinel}):
            assert sys.modules["_mock_helpers_probe_f"].configure_structlog() is marker


class TestRestoreSemantics:
    """``__exit__`` restores the prior ``sys.modules`` state."""

    def test_pre_existing_entry_restored(self) -> None:
        previous = MagicMock()
        sys.modules["_mock_helpers_probe_g"] = previous
        try:
            with mock_sys_modules(["_mock_helpers_probe_g"]) as installed:
                assert (
                    sys.modules["_mock_helpers_probe_g"]
                    is installed["_mock_helpers_probe_g"]
                )
                assert sys.modules["_mock_helpers_probe_g"] is not previous
            assert sys.modules["_mock_helpers_probe_g"] is previous
        finally:
            sys.modules.pop("_mock_helpers_probe_g", None)

    def test_absent_entry_deleted_on_exit(self) -> None:
        assert "_mock_helpers_probe_h" not in sys.modules
        with mock_sys_modules(["_mock_helpers_probe_h"]):
            assert "_mock_helpers_probe_h" in sys.modules
        assert "_mock_helpers_probe_h" not in sys.modules

    def test_restore_runs_even_when_body_raises(self) -> None:
        """The ``with`` body inside a real test file is the
        ``import script_under_test`` line; if that raises (a syntax error,
        an attribute access on the mock that doesn't behave as expected,
        a transitive ``ImportError``) we still need ``sys.modules`` clean
        for downstream tests. The contextmanager's ``finally`` clause
        provides this guarantee — pin it.
        """
        assert "_mock_helpers_probe_i" not in sys.modules

        class _Boom(Exception):
            pass

        try:
            with mock_sys_modules(["_mock_helpers_probe_i"]):
                assert "_mock_helpers_probe_i" in sys.modules
                raise _Boom("simulated import failure")
        except _Boom:
            pass

        assert "_mock_helpers_probe_i" not in sys.modules

    def test_pre_existing_entry_restored_even_when_body_raises(self) -> None:
        previous = MagicMock()
        sys.modules["_mock_helpers_probe_j"] = previous

        class _Boom(Exception):
            pass

        try:
            try:
                with mock_sys_modules(["_mock_helpers_probe_j"]):
                    assert sys.modules["_mock_helpers_probe_j"] is not previous
                    raise _Boom("simulated import failure")
            except _Boom:
                pass

            assert sys.modules["_mock_helpers_probe_j"] is previous
        finally:
            sys.modules.pop("_mock_helpers_probe_j", None)


class TestNestedUsage:
    """Two ``with`` blocks reusing one of the same names should leave
    ``sys.modules`` in the correct intermediate state on each exit.
    """

    def test_nested_same_name_restores_outer_mock(self) -> None:
        outer = MagicMock()
        inner = MagicMock()

        assert "_mock_helpers_probe_k" not in sys.modules

        with mock_sys_modules({"_mock_helpers_probe_k": outer}):
            assert sys.modules["_mock_helpers_probe_k"] is outer
            with mock_sys_modules({"_mock_helpers_probe_k": inner}):
                assert sys.modules["_mock_helpers_probe_k"] is inner
            # Inner exits — outer's mock should be restored.
            assert sys.modules["_mock_helpers_probe_k"] is outer

        # Outer exits — entry should be gone.
        assert "_mock_helpers_probe_k" not in sys.modules


class TestEmptyInput:
    """An empty iterable / dict is a no-op (degenerate but legal)."""

    def test_empty_iterable_is_noop(self) -> None:
        before = dict(sys.modules)
        with mock_sys_modules([]) as installed:
            assert installed == {}
        assert dict(sys.modules) == before

    def test_empty_mapping_is_noop(self) -> None:
        before = dict(sys.modules)
        with mock_sys_modules({}) as installed:
            assert installed == {}
        assert dict(sys.modules) == before


class TestNoLeakInvariant:
    """Every shape of usage (iterable, mapping, success, exception, nested)
    leaves the named modules out of ``sys.modules`` on exit. This is the
    high-level invariant ``test_scripts_tests_isolation.py`` enforces at
    the cross-file scope; this test pins it at the helper scope so a
    regression in ``_mock_helpers.py`` is caught earlier with a clearer
    failure message.
    """

    def test_no_leak_after_context_exits(self) -> None:
        for shape in (
            ["_mock_helpers_probe_l"],
            {"_mock_helpers_probe_l": MagicMock()},
        ):
            assert "_mock_helpers_probe_l" not in sys.modules
            with mock_sys_modules(shape):
                assert "_mock_helpers_probe_l" in sys.modules
            assert "_mock_helpers_probe_l" not in sys.modules
