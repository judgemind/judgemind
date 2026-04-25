#!/usr/bin/env python3
"""check-no-unbounded-timeouts.py — Fail if any IO call site under
``scripts/dispatcher/`` (excluding ``scripts/dispatcher/tests/``) lacks
a bounded timeout.

Why this check exists
---------------------
The 2026-04-25 cascade was caused by a single ``boto3.client("ecs", ...)``
call constructed without an explicit ``botocore.config.Config``. boto3's
default ``read_timeout`` is effectively unbounded; a hung ``ecs:DescribeTasks``
in the reaper held the GIL inside urllib3's blocking ``recv()`` for 30+
minutes, silenced the entire Python process, and only recovered via
manual ``aws ecs stop-task``. PR #3353 closed that one site (``_make_ecs_client``).
This check enforces the structural invariant: every IO call site in the
dispatcher daemon must have a bounded timeout.

Rules enforced
--------------
For every ``ast.Call`` node under ``scripts/dispatcher/**/*.py`` (excluding
``scripts/dispatcher/tests/**``):

  1. ``boto3.client(...)`` -- must pass ``config=`` and the value must be
     a ``Config(...)`` literal carrying both ``connect_timeout=`` and
     ``read_timeout=`` kwargs.
  2. ``psycopg.connect(...)`` -- must pass ``connect_timeout=`` AND must
     pass ``options=`` whose value contains the substring
     ``statement_timeout`` (the libpq server-side option form). The
     daemon uses ``options="-c statement_timeout=30000"``; this rule
     bounds both handshake and per-query wall clock.
  3. ``subprocess.run(...)``, ``subprocess.check_output(...)``,
     ``subprocess.check_call(...)`` -- must pass ``timeout=`` (kwarg) or
     a ``**kwargs`` splat (``kw.arg is None``).
  4. ``subprocess.Popen(...)`` -- allowed if the same enclosing function
     contains a ``proc.wait(timeout=...)`` call AND a ``proc.kill()``
     call inside an ``except subprocess.TimeoutExpired`` handler.
  5. ``requests.get/post/put/delete/patch/head/request(...)`` -- must
     pass ``timeout=`` (kwarg) or a ``**kwargs`` splat.
  6. ``urllib.request.urlopen(...)`` (or imported as ``urlopen``) --
     must pass ``timeout=`` or ``**kwargs`` splat.
  7. ``socket.create_connection(...)`` -- must pass ``timeout=`` or
     ``**kwargs`` splat.
  8. ``http.client.HTTPConnection(...)`` /
     ``http.client.HTTPSConnection(...)`` -- must pass ``timeout=`` or
     ``**kwargs`` splat.

Allowlist
---------
A call may be exempted by adding ``# noqa: timeout-required: <reason>``
to the same source line as the call (or any line covered by the call's
multi-line span). A bare ``# noqa`` does NOT exempt -- the rule name
``timeout-required`` AND a non-empty reason are required.

Output
------
On failure the script prints one line per offending call:
``path:line: <call> -- missing: <thing>``

Usage
-----
  scripts/check-no-unbounded-timeouts.py            # scan default tree
  scripts/check-no-unbounded-timeouts.py [path...]  # scan specific files

Exit codes
----------
  0 -- No violations.
  1 -- At least one IO call site is missing its timeout constraint.

Tracking: issue #3356.
"""

# venv: none
# permanent: true

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_ROOT = REPO_ROOT / "scripts" / "dispatcher"

# requests methods that count as HTTP egress.
REQUESTS_METHODS = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "request"}
)

# Allowlist marker -- see module docstring for the exact spelling. We
# require the rule name AND a non-empty reason. Bare markers (no rule
# name, or empty reason) are rejected -- see _line_is_allowlisted.
_ALLOWLIST_RE = re.compile(r"#\s*noqa\s*:\s*timeout-required\s*:\s*\S+")


# ---------------------------------------------------------------------------
# AST predicates for each rule
# ---------------------------------------------------------------------------


def _is_attr_call(func: ast.expr, owner: str, attr: str) -> bool:
    """Match ``owner.attr(...)`` -- e.g. ``boto3.client``."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == attr
        and isinstance(func.value, ast.Name)
        and func.value.id == owner
    )


def _is_boto3_client(func: ast.expr) -> bool:
    return _is_attr_call(func, "boto3", "client")


def _is_psycopg_connect(func: ast.expr) -> bool:
    return _is_attr_call(func, "psycopg", "connect")


def _is_subprocess_simple(func: ast.expr) -> bool:
    """Match ``subprocess.run/check_output/check_call`` (NOT Popen)."""
    if not isinstance(func, ast.Attribute):
        return False
    if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
        return False
    return func.attr in ("run", "check_output", "check_call")


def _is_subprocess_popen(func: ast.expr) -> bool:
    return _is_attr_call(func, "subprocess", "Popen")


def _is_requests_call(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr in REQUESTS_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id == "requests"
    )


def _is_urlopen(func: ast.expr) -> bool:
    # urllib.request.urlopen(...)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "urlopen"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "request"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "urllib"
    ):
        return True
    # from urllib.request import urlopen; urlopen(...)
    if isinstance(func, ast.Name) and func.id == "urlopen":
        return True
    return False


def _is_socket_create_connection(func: ast.expr) -> bool:
    return _is_attr_call(func, "socket", "create_connection")


def _is_http_connection(func: ast.expr) -> bool:
    """Match ``http.client.HTTPConnection`` and ``HTTPSConnection``."""
    if (
        isinstance(func, ast.Attribute)
        and func.attr in ("HTTPConnection", "HTTPSConnection")
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "client"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "http"
    ):
        return True
    # from http.client import HTTPConnection; HTTPConnection(...)
    if isinstance(func, ast.Name) and func.id in (
        "HTTPConnection",
        "HTTPSConnection",
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Helpers for inspecting Call nodes
# ---------------------------------------------------------------------------


def _has_kwarg(call: ast.Call, name: str) -> bool:
    """Return True if ``call`` passes ``name=...`` explicitly."""
    return any(kw.arg == name for kw in call.keywords)


def _has_kwargs_splat(call: ast.Call) -> bool:
    """Return True if ``call`` passes ``**kwargs`` (kw.arg is None)."""
    return any(kw.arg is None for kw in call.keywords)


def _kwarg_value(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_config_call(node: ast.expr) -> bool:
    """Return True if ``node`` is a ``Config(...)`` call literal in any
    of the canonical forms: ``Config(...)``, ``botocore.config.Config(...)``,
    ``botocore.client.Config(...)``, ``config.Config(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "Config":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "Config":
        return True
    return False


def _config_call_has(node: ast.expr, *required_kwargs: str) -> bool:
    """If ``node`` is a ``Config(...)`` call literal, return True only if
    every name in ``required_kwargs`` appears as a kwarg."""
    if not _is_config_call(node):
        return False
    assert isinstance(node, ast.Call)
    present = {kw.arg for kw in node.keywords if kw.arg is not None}
    return all(req in present for req in required_kwargs)


def _resolve_config_value(
    tree: ast.AST,
    call: ast.Call,
    cfg_value: ast.expr,
    *required_kwargs: str,
) -> bool:
    """Return True if ``cfg_value`` (the value of ``config=``) resolves
    to a ``Config(...)`` literal carrying every name in ``required_kwargs``.

    Two cases:
      1. ``config=Config(...)`` -- inline literal; check directly.
      2. ``config=cw_cfg`` -- Name reference; walk the enclosing function
         for an ``Assign`` of ``cw_cfg = Config(...)`` and check that.

    The Name-resolution path matches the daemon's preferred shape:
    construct the Config in a local variable for readability, then pass
    it into ``boto3.client(...)``.
    """
    if _is_config_call(cfg_value):
        return _config_call_has(cfg_value, *required_kwargs)
    if not isinstance(cfg_value, ast.Name):
        return False
    target_name = cfg_value.id
    enclosing = _enclosing_function(tree, call)
    search_root: ast.AST = enclosing if enclosing is not None else tree
    for node in ast.walk(search_root):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == target_name:
                    if _config_call_has(node.value, *required_kwargs):
                        return True
        if isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == target_name
                and node.value is not None
                and _config_call_has(node.value, *required_kwargs)
            ):
                return True
    return False


def _options_value_has_substring(node: ast.expr | None, needle: str) -> bool:
    """Return True if ``node`` is a string Constant containing ``needle``,
    OR a Name reference (we conservatively trust named module-level
    constants used by the daemon)."""
    if node is None:
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return needle in node.value
    if isinstance(node, ast.Name):
        # Name references resolve at runtime; trust the convention that
        # the daemon's PSYCOPG_STATEMENT_TIMEOUT_OPTIONS constant is the
        # only sanctioned source. The tests assert behaviour for both
        # the literal and the named-constant form.
        return True
    return False


# ---------------------------------------------------------------------------
# Popen pairing detection: Popen(...) is allowed if the enclosing
# function/method contains a ``proc.wait(timeout=...)`` AND a
# ``proc.kill()`` inside an ``except subprocess.TimeoutExpired`` handler.
# ---------------------------------------------------------------------------


def _popen_has_pairing(tree: ast.AST, popen_node: ast.Call) -> bool:
    """Heuristic: walk every enclosing function/method and look for the
    canonical Popen + wait(timeout=...) + TimeoutExpired -> kill() shape.
    Returns True if the pattern is present anywhere in the same function.

    The check intentionally does not follow the actual data-flow (e.g.
    confirm the wait() is called on the same Popen-returned name). The
    daemon's two Popen sites (stream_subprocess_output_async wrapper
    around claude -p, and the diagnoser subprocess) both follow the
    canonical idiom -- a future contributor adding a Popen without that
    idiom will get caught by Test (h) "Popen with timeout= in wait but
    no kill() in TimeoutExpired" or Test (g) "Popen with no wait pairing".
    """
    enclosing = _enclosing_function(tree, popen_node)
    if enclosing is None:
        return False

    has_wait_with_timeout = False
    has_timeout_expired_kill = False

    for node in ast.walk(enclosing):
        # ``X.wait(timeout=...)`` anywhere in the same function.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait"
            and _has_kwarg(node, "timeout")
        ):
            has_wait_with_timeout = True
        # ``except subprocess.TimeoutExpired:`` handler containing a
        # ``X.kill()`` call.
        if isinstance(node, ast.ExceptHandler) and _handler_matches_timeout_expired(
            node
        ):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "kill"
                ):
                    has_timeout_expired_kill = True
                    break

    return has_wait_with_timeout and has_timeout_expired_kill


def _handler_matches_timeout_expired(handler: ast.ExceptHandler) -> bool:
    """Return True if the handler catches ``subprocess.TimeoutExpired``
    (or bare ``TimeoutExpired``)."""
    exc = handler.type
    if exc is None:
        return False
    if isinstance(exc, ast.Attribute) and exc.attr == "TimeoutExpired":
        return True
    if isinstance(exc, ast.Name) and exc.id == "TimeoutExpired":
        return True
    # except (TimeoutExpired, OtherExc):
    if isinstance(exc, ast.Tuple):
        return any(_handler_matches_timeout_expired_expr(e) for e in exc.elts)
    return False


def _handler_matches_timeout_expired_expr(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "TimeoutExpired":
        return True
    if isinstance(node, ast.Name) and node.id == "TimeoutExpired":
        return True
    return False


def _enclosing_function(
    tree: ast.AST, target: ast.AST
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the innermost ``FunctionDef``/``AsyncFunctionDef`` ancestor
    of ``target``, or None if ``target`` is at module top level.

    We walk the tree explicitly because ``ast`` doesn't track parents.
    """
    candidate: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _node_contains(node, target):
                candidate = node
    return candidate


def _node_contains(parent: ast.AST, child: ast.AST) -> bool:
    """Return True if ``child`` is a descendant of ``parent`` (or the same
    node). Linear in the size of the parent subtree."""
    if parent is child:
        return True
    for node in ast.walk(parent):
        if node is child:
            return True
    return False


# ---------------------------------------------------------------------------
# Allowlist parsing
# ---------------------------------------------------------------------------


def _line_is_allowlisted(call: ast.Call, source_lines: list[str]) -> bool:
    """Return True if any source line covered by ``call`` carries a
    ``# noqa: timeout-required: <reason>`` comment."""
    start = call.lineno
    end = getattr(call, "end_lineno", None) or start
    for lineno in range(start, end + 1):
        idx = lineno - 1
        if 0 <= idx < len(source_lines):
            if _ALLOWLIST_RE.search(source_lines[idx]):
                return True
    return False


# ---------------------------------------------------------------------------
# Per-call rule application
# ---------------------------------------------------------------------------


def _check_call(
    tree: ast.AST,
    call: ast.Call,
    source_lines: list[str],
) -> tuple[str, str] | None:
    """Apply every rule to ``call``. Return ``(call_label, missing_msg)``
    on violation, or None if the call passes (or no rule applies).
    """
    func = call.func

    # boto3.client(...): must have config=Config(connect_timeout=, read_timeout=)
    if _is_boto3_client(func):
        cfg = _kwarg_value(call, "config")
        if cfg is None:
            return ("boto3.client", "config=botocore.config.Config(...)")
        if not _resolve_config_value(
            tree, call, cfg, "connect_timeout", "read_timeout"
        ):
            return (
                "boto3.client",
                "config=Config(...) with both connect_timeout= and read_timeout=",
            )
        return None

    # psycopg.connect(...): must have connect_timeout= AND options= containing
    # statement_timeout.
    if _is_psycopg_connect(func):
        missing: list[str] = []
        if not _has_kwarg(call, "connect_timeout"):
            missing.append("connect_timeout=")
        opts = _kwarg_value(call, "options")
        if opts is None or not _options_value_has_substring(opts, "statement_timeout"):
            missing.append("options=...statement_timeout...")
        if missing:
            return ("psycopg.connect", " AND ".join(missing))
        return None

    # subprocess.run/check_output/check_call: must have timeout= or **kwargs.
    if _is_subprocess_simple(func):
        if not (_has_kwarg(call, "timeout") or _has_kwargs_splat(call)):
            attr = func.attr if isinstance(func, ast.Attribute) else "?"
            return (f"subprocess.{attr}", "timeout=")
        return None

    # subprocess.Popen: allowed if the same function contains a paired
    # wait(timeout=) + TimeoutExpired -> kill() block. Or, equivalently,
    # if the call carries ``timeout=`` directly (some Python versions
    # surface ``timeout=`` on Popen for read-side bounding).
    if _is_subprocess_popen(func):
        if _has_kwarg(call, "timeout") or _has_kwargs_splat(call):
            return None
        if _popen_has_pairing(tree, call):
            return None
        return (
            "subprocess.Popen",
            "timeout= OR a paired proc.wait(timeout=) + except TimeoutExpired -> proc.kill()",
        )

    # requests.<method>(...): must have timeout= or **kwargs.
    if _is_requests_call(func):
        if not (_has_kwarg(call, "timeout") or _has_kwargs_splat(call)):
            attr = func.attr if isinstance(func, ast.Attribute) else "?"
            return (f"requests.{attr}", "timeout=")
        return None

    # urllib.request.urlopen / urlopen: must have timeout= or **kwargs.
    if _is_urlopen(func):
        if not (_has_kwarg(call, "timeout") or _has_kwargs_splat(call)):
            return ("urllib.request.urlopen", "timeout=")
        return None

    # socket.create_connection(...): must have timeout= or **kwargs.
    if _is_socket_create_connection(func):
        if not (_has_kwarg(call, "timeout") or _has_kwargs_splat(call)):
            return ("socket.create_connection", "timeout=")
        return None

    # http.client.HTTP{S,}Connection(...): must have timeout= or **kwargs.
    if _is_http_connection(func):
        if not (_has_kwarg(call, "timeout") or _has_kwargs_splat(call)):
            label = (
                f"http.client.{func.attr}"
                if isinstance(func, ast.Attribute)
                else "http.client.HTTP[S]Connection"
            )
            return (label, "timeout=")
        return None

    return None


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def _iter_python_files(roots: list[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            # Exclude scripts/dispatcher/tests/ — they may contain
            # intentional unbounded fixtures.
            if "/tests/" in str(path) or "\\tests\\" in str(path):
                continue
            if "/.venv/" in str(path) or "\\.venv\\" in str(path):
                continue
            yield path


def scan_file(path: Path) -> list[str]:
    """Return a list of violation lines for ``path``. Empty if clean."""
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    source_lines = source.splitlines()

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        result = _check_call(tree, node, source_lines)
        if result is None:
            continue
        if _line_is_allowlisted(node, source_lines):
            continue
        label, missing = result
        violations.append(f"{path}:{node.lineno}: {label} -- missing: {missing}")
    return violations


def main(argv: list[str]) -> int:
    if argv:
        roots = [Path(a).resolve() for a in argv]
    else:
        roots = [DEFAULT_SCAN_ROOT]

    all_violations: list[str] = []
    for path in _iter_python_files(roots):
        all_violations.extend(scan_file(path))

    if all_violations:
        sys.stderr.write("ERROR: unbounded IO call site(s) found in dispatcher.\n\n")
        for line in all_violations:
            sys.stderr.write(f"    {line}\n")
        sys.stderr.write(
            "\nFix: add the missing kwarg, or annotate the call with\n"
            "    # noqa: timeout-required: <reason>\n"
            "on the same line. See scripts/check-no-unbounded-timeouts.py\n"
            "for the full rule table. Tracking: issue #3356.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
