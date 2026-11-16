"""Fingerprints of the code behind a versioned verdict (#5134).

``validation.skip_versions.EXTRACTION_SKIP_RULE_VERSION`` and
``validation.rule_versions.RULE_VERSIONS`` are hand-maintained numbers: the
reconciler re-judges a recorded verdict only when its version moves.  The
fingerprint tests pin a hash of the code each version stands for, so a change
to that code fails a test until its author bumps the version or repins with
a reason the verdicts cannot change.

A function is hashed by its normalized AST (docstrings stripped, positions
dropped), so a comment, formatting or docstring edit leaves it unchanged and
a logic edit does not.  A constant is hashed by its value.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import re
import sys
import textwrap
from collections.abc import Callable, Iterable
from typing import Any

#: Value types hashed as constants when a covered function names them.
_CONSTANT_TYPES = (int, float, str, bytes, bool, tuple, list, dict, frozenset, set, re.Pattern)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _tree_fingerprint(tree: ast.AST) -> str:
    # Python 3.13 drops empty fields from ``ast.dump`` by default; keep the
    # 3.12 form the pins were taken with.
    extra: dict[str, Any] = {"show_empty": True} if sys.version_info >= (3, 13) else {}
    return _digest(ast.dump(tree, annotate_fields=True, include_attributes=False, **extra))


def source_fingerprint(source: str) -> str:
    """The fingerprint of a function's *source*: its normalized AST."""
    return _tree_fingerprint(_strip_docstrings(ast.parse(textwrap.dedent(source))))


def _normalized(value: Any) -> Any:
    """*value* in a form whose ``repr`` is complete (a long pattern's
    ``repr`` is truncated)."""
    if isinstance(value, re.Pattern):
        return ("re", value.pattern, int(value.flags))
    if isinstance(value, dict):
        return ("dict", sorted((repr(k), _normalized(v)) for k, v in value.items()))
    if isinstance(value, set | frozenset):
        return ("set", sorted(repr(_normalized(v)) for v in value))
    if isinstance(value, list | tuple):
        return (type(value).__name__, [_normalized(v) for v in value])
    return value


def value_fingerprint(value: Any) -> str:
    """The fingerprint of a constant's *value*."""
    return _digest(repr(_normalized(value)))


def _node_fingerprint(node: ast.AST) -> str:
    """The fingerprint of a top-level function *node* (docstrings already
    stripped); equal to :func:`source_fingerprint` of its source."""
    return _tree_fingerprint(ast.Module(body=[node], type_ignores=[]))  # type: ignore[list-item]


def _module_functions(module: str) -> dict[str, ast.AST]:
    """The top-level function definitions of *module*, read from its source
    file (a test that patches a module attribute does not change them)."""
    path = inspect.getsourcefile(importlib.import_module(module))
    assert path, module
    with open(path, encoding="utf-8") as fh:
        tree = _strip_docstrings(ast.parse(fh.read()))
    return {
        node.name: node
        for node in tree.body  # type: ignore[attr-defined]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def closure_fingerprints(
    roots: Iterable[Callable[..., Any]], modules: Iterable[str]
) -> dict[str, str]:
    """Fingerprints of *roots* and, transitively, of the functions and
    constants they name that live in *modules*: ``{"module.name": hash}``.

    Functions are read from their module's source; a name a covered
    function uses is followed when it is a top-level function of the same
    module, a function imported from another of *modules*, or a constant
    (hashed by its current value)."""
    allowed = set(modules)
    functions: dict[str, dict[str, ast.AST]] = {}
    out: dict[str, str] = {}
    stack = [(fn.__module__, fn.__name__) for fn in roots]
    while stack:
        module, name = stack.pop()
        key = f"{module}.{name}"
        if key in out:
            continue
        if module not in functions:
            functions[module] = _module_functions(module)
        node = functions[module][name]
        out[key] = _node_fingerprint(node)
        namespace = vars(importlib.import_module(module))
        refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        for ref in sorted(refs):
            if ref in functions[module]:
                stack.append((module, ref))
                continue
            if ref not in namespace:
                continue
            value = namespace[ref]
            if inspect.isfunction(value):
                if value.__module__ in allowed and value.__module__ != module:
                    stack.append((value.__module__, value.__name__))
            elif isinstance(value, _CONSTANT_TYPES):
                out.setdefault(f"{module}.{ref}", value_fingerprint(value))
    return out


def changed(pinned: dict[str, str], computed: dict[str, str]) -> list[str]:
    """The covered objects whose fingerprint differs from the pin (added,
    removed or edited), sorted."""
    names = set(pinned) | set(computed)
    return sorted(n for n in names if pinned.get(n) != computed.get(n))


def format_pins(computed: dict[str, str]) -> str:
    """*computed* as a Python dict literal, to paste as the new pin."""
    lines = [f'    "{name}": "{digest}",' for name, digest in sorted(computed.items())]
    return "{\n" + "\n".join(lines) + "\n}"
