"""Tests for remove_test_functions module."""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remove_test_functions import (
    find_node_ranges,
    remove_functions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedent(code: str) -> str:
    """Dedent and strip leading/trailing blank lines from test code."""
    return textwrap.dedent(code).strip() + "\n"


def _assert_valid_python(code: str) -> None:
    """Assert that the given string is valid Python."""
    ast.parse(code)


# ---------------------------------------------------------------------------
# Tests for find_node_ranges
# ---------------------------------------------------------------------------


class TestFindNodeRanges:
    """Tests for find_node_ranges — AST-based boundary detection."""

    def test_simple_function(self) -> None:
        source = _dedent("""\
            def helper():
                pass

            def target():
                return 1

            def keeper():
                return 2
        """)
        ranges = find_node_ranges(source, ["target"])
        assert len(ranges) == 1
        # Should cover lines 4-5 (the target function)
        start, end = ranges[0]
        lines = source.splitlines()
        assert lines[start - 1].strip() == "def target():"
        assert lines[end - 1].strip() == "return 1"

    def test_decorated_function(self) -> None:
        source = _dedent("""\
            import unittest.mock

            @unittest.mock.patch("os.path.exists")
            @unittest.mock.patch("os.getcwd")
            def test_something():
                pass

            def test_other():
                pass
        """)
        ranges = find_node_ranges(source, ["test_something"])
        assert len(ranges) == 1
        start, end = ranges[0]
        lines = source.splitlines()
        # Should include decorators
        assert "@unittest.mock.patch" in lines[start - 1]
        assert lines[end - 1].strip() == "pass"

    def test_class_removal(self) -> None:
        source = _dedent("""\
            class TestKeep:
                def test_a(self):
                    pass

            class TestRemove:
                def test_b(self):
                    pass

            class TestAlsoKeep:
                def test_c(self):
                    pass
        """)
        ranges = find_node_ranges(source, ["TestRemove"])
        assert len(ranges) == 1
        start, end = ranges[0]
        lines = source.splitlines()
        assert "class TestRemove" in lines[start - 1]
        assert lines[end - 1].strip() == "pass"

    def test_nested_method_removal(self) -> None:
        source = _dedent("""\
            class TestSuite:
                def test_keep(self):
                    return 1

                def test_remove(self):
                    return 2

                def test_also_keep(self):
                    return 3
        """)
        ranges = find_node_ranges(source, ["TestSuite.test_remove"])
        assert len(ranges) == 1
        start, end = ranges[0]
        lines = source.splitlines()
        assert "def test_remove" in lines[start - 1]
        assert lines[end - 1].strip() == "return 2"

    def test_multiple_removals(self) -> None:
        source = _dedent("""\
            def func_a():
                pass

            def func_b():
                pass

            def func_c():
                pass
        """)
        ranges = find_node_ranges(source, ["func_a", "func_c"])
        assert len(ranges) == 2

    def test_unknown_name_raises(self) -> None:
        source = _dedent("""\
            def existing():
                pass
        """)
        with pytest.raises(ValueError, match="not_found"):
            find_node_ranges(source, ["not_found"])

    def test_decorated_method_in_class(self) -> None:
        source = _dedent("""\
            class TestSuite:
                @pytest.mark.parametrize("x", [1, 2])
                def test_decorated(self):
                    pass

                def test_plain(self):
                    pass
        """)
        ranges = find_node_ranges(source, ["TestSuite.test_decorated"])
        assert len(ranges) == 1
        start, _end = ranges[0]
        lines = source.splitlines()
        assert "@pytest.mark.parametrize" in lines[start - 1]


# ---------------------------------------------------------------------------
# Tests for remove_functions (integration)
# ---------------------------------------------------------------------------


class TestRemoveFunctions:
    """Integration tests for the full removal pipeline."""

    def test_basic_removal(self) -> None:
        source = _dedent("""\
            def keep():
                return 1

            def remove_me():
                return 2

            def also_keep():
                return 3
        """)
        result = remove_functions(source, ["remove_me"])
        _assert_valid_python(result)
        assert "def keep():" in result
        assert "def also_keep():" in result
        assert "def remove_me():" not in result

    def test_preserves_imports(self) -> None:
        source = _dedent("""\
            from __future__ import annotations

            import os
            import sys

            def remove_me():
                os.getcwd()

            def keep_me():
                sys.exit(0)
        """)
        result = remove_functions(source, ["remove_me"])
        _assert_valid_python(result)
        assert "import os" in result
        assert "import sys" in result
        assert "def keep_me():" in result
        assert "def remove_me():" not in result

    def test_class_method_removal_preserves_class(self) -> None:
        source = _dedent("""\
            class TestSuite:
                _doc_meta = {"key": "value"}

                def test_keep(self):
                    return 1

                def test_remove(self):
                    return 2

                def test_also_keep(self):
                    return 3
        """)
        result = remove_functions(source, ["TestSuite.test_remove"])
        _assert_valid_python(result)
        assert "class TestSuite:" in result
        assert "_doc_meta" in result
        assert "def test_keep(self):" in result
        assert "def test_also_keep(self):" in result
        assert "def test_remove(self):" not in result

    def test_decorated_function_removal(self) -> None:
        source = _dedent("""\
            from unittest.mock import patch

            @patch("os.path.exists")
            @patch("os.getcwd")
            def test_patched():
                pass

            def test_next():
                pass
        """)
        result = remove_functions(source, ["test_patched"])
        _assert_valid_python(result)
        assert "def test_next():" in result
        assert "def test_patched():" not in result
        assert "@patch" not in result

    def test_output_passes_ast_parse(self) -> None:
        source = _dedent("""\
            class TestA:
                def test_one(self):
                    x = 1
                    return x

                @pytest.fixture()
                def my_fixture(self):
                    yield 42

                def test_two(self):
                    pass

            def standalone():
                pass
        """)
        result = remove_functions(source, ["TestA.my_fixture", "standalone"])
        _assert_valid_python(result)
        assert "class TestA:" in result
        assert "def test_one(self):" in result
        assert "def test_two(self):" in result
        assert "def my_fixture(self):" not in result
        assert "def standalone():" not in result

    def test_remove_entire_class(self) -> None:
        source = _dedent("""\
            class TestKeep:
                def test_a(self):
                    pass

            class TestRemove:
                def test_b(self):
                    pass

                def test_c(self):
                    pass

            def standalone():
                pass
        """)
        result = remove_functions(source, ["TestRemove"])
        _assert_valid_python(result)
        assert "class TestKeep:" in result
        assert "class TestRemove:" not in result
        assert "def standalone():" in result

    def test_no_excessive_blank_lines(self) -> None:
        source = _dedent("""\
            def func_a():
                pass

            def func_b():
                pass

            def func_c():
                pass
        """)
        result = remove_functions(source, ["func_b"])
        _assert_valid_python(result)
        # Should not have more than 2 consecutive blank lines
        assert "\n\n\n\n" not in result

    def test_nested_class_removal(self) -> None:
        source = _dedent("""\
            class Outer:
                class Inner:
                    def test_inner(self):
                        pass

                def test_outer(self):
                    pass
        """)
        result = remove_functions(source, ["Outer.Inner"])
        _assert_valid_python(result)
        assert "class Outer:" in result
        assert "class Inner:" not in result
        assert "def test_outer(self):" in result

    def test_async_function_removal(self) -> None:
        source = _dedent("""\
            async def test_async_remove():
                await something()

            async def test_async_keep():
                await other()
        """)
        result = remove_functions(source, ["test_async_remove"])
        _assert_valid_python(result)
        assert "async def test_async_keep():" in result
        assert "async def test_async_remove():" not in result

    def test_multiline_decorator_removal(self) -> None:
        source = _dedent("""\
            @pytest.mark.parametrize(
                "x,y",
                [(1, 2), (3, 4)],
            )
            def test_param(x, y):
                assert x < y

            def test_other():
                pass
        """)
        result = remove_functions(source, ["test_param"])
        _assert_valid_python(result)
        assert "def test_other():" in result
        assert "def test_param(" not in result
        assert "@pytest.mark.parametrize" not in result

    def test_empty_class_after_removal_gets_pass(self) -> None:
        source = _dedent("""\
            class TestSuite:
                def test_only_method(self):
                    return 1
        """)
        result = remove_functions(source, ["TestSuite.test_only_method"])
        _assert_valid_python(result)
        assert "class TestSuite:" in result
        # Class must still be valid — needs a pass statement or similar
        tree = ast.parse(result)
        class_node = tree.body[0]
        assert isinstance(class_node, ast.ClassDef)
        assert len(class_node.body) > 0


# ---------------------------------------------------------------------------
# Tests for CLI (main function)
# ---------------------------------------------------------------------------


class TestCLI:
    """Tests for the command-line interface."""

    def test_dry_run(self, tmp_path: Path) -> None:
        source = _dedent("""\
            def keep():
                pass

            def remove_me():
                pass
        """)
        target = tmp_path / "test_file.py"
        target.write_text(source)

        from remove_test_functions import main

        exit_code = main(["--dry-run", str(target), "--names", "remove_me"])
        assert exit_code == 0
        # File should not be modified in dry-run mode
        assert target.read_text() == source

    def test_in_place_modification(self, tmp_path: Path) -> None:
        source = _dedent("""\
            def keep():
                pass

            def remove_me():
                pass
        """)
        target = tmp_path / "test_file.py"
        target.write_text(source)

        from remove_test_functions import main

        exit_code = main([str(target), "--names", "remove_me"])
        assert exit_code == 0
        result = target.read_text()
        assert "def keep():" in result
        assert "def remove_me():" not in result
        _assert_valid_python(result)

    def test_missing_name_exits_with_error(self, tmp_path: Path) -> None:
        source = _dedent("""\
            def existing():
                pass
        """)
        target = tmp_path / "test_file.py"
        target.write_text(source)

        from remove_test_functions import main

        exit_code = main([str(target), "--names", "nonexistent"])
        assert exit_code == 1
