#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-dispatcher-image-deps.py — Verify that every non-stdlib import in
``scripts/dispatcher/**/*.py`` has a matching ``pip install`` entry in
``Dockerfile.dispatcher``.

Motivation (#2776): PR #2771 (Phase 2 dispatcher daemon) added
``import boto3`` inside ``_make_cloudwatch_client`` as a lazy import but
did not add ``boto3`` to ``Dockerfile.dispatcher``'s pip install step.
The daemon unit tests mock the client by method assignment, so no test
failed — yet every supervisor tick in production logged
``daemon.heartbeat_metric_failed: No module named 'boto3'`` for ~24h
until it was noticed and filed as #2773.

The dispatcher is a single entrypoint (``scripts/dispatcher/daemon.py``)
running in a single container image (``Dockerfile.dispatcher``). That
narrow shape makes a static "imports vs pip install" diff cheap and
reliable — which is exactly what this script provides.

What the check does
───────────────────
1. Walk ``scripts/dispatcher/*.py`` (non-recursive into ``tests/`` —
   test files may import pytest, mocks, etc. that legitimately are not
   in the production image). The ``__init__.py`` and ``daemon.py`` /
   ``emit_failure.py`` modules are the ones that run inside the image.
2. Parse every file with ``ast.parse`` and collect the top-level
   package name from every ``Import`` and ``ImportFrom`` node —
   including lazy imports inside function bodies (``boto3`` is one).
3. Drop:
   - stdlib modules (``sys.stdlib_module_names``),
   - the dispatcher's own sibling imports (``from scripts.dispatcher..``
     / ``from .emit_failure``),
   - ``__future__``.
4. Parse every ``pip install`` RUN step in ``Dockerfile.dispatcher`` and
   collect the installed package names (stripping version specifiers,
   quotes, and ``[extras]``).
5. Map each imported top-level name to its pip package name via a small
   explicit table (``boto3`` → ``boto3``, ``psycopg`` → ``psycopg``,
   ``yaml`` → ``PyYAML``, ``cv2`` → ``opencv-python``). Import names
   that are not in the table default to themselves — that covers the
   common case where the import name IS the pip name.
6. Fail if any mapped pip name is not in the installed set. Exit 0 on
   success.

The import-name → pip-name table is intentionally short: the dispatcher
has a tight dependency list, so only the handful of known mismatched
names need an entry. Adding a new dispatcher dep in the future means
adding both the pip install line in the Dockerfile AND (if the import
name differs) an entry in ``IMPORT_TO_PACKAGE``.

Usage
─────
    scripts/check-dispatcher-image-deps.py                        # default paths
    scripts/check-dispatcher-image-deps.py --dockerfile PATH      # override Dockerfile path
    scripts/check-dispatcher-image-deps.py --source-dir PATH      # override source dir
    scripts/check-dispatcher-image-deps.py --help

Exit codes
──────────
    0   All imports resolve to packages installed in the Dockerfile.
    1   At least one import is missing from the pip install step.
    2   CLI / IO error (bad arguments, unreadable file, parse error).

Testing
───────
See ``scripts/tests/test_check_dispatcher_image_deps.sh`` for the peer
shell test. Tests cover: (a) happy path on current main passes;
(b) a synthesized missing dep fails; (c) a lazy import inside a function
body is caught; (d) stdlib imports do not produce false positives;
(e) dispatcher-sibling imports are ignored.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import-name → pip-package-name table.
#
# Keep this short. Only entries whose import name does NOT match the pip
# package name belong here. For every other import, the import name is
# assumed to equal the pip name (so ``psycopg`` → ``psycopg``,
# ``boto3`` → ``boto3``).
#
# The three entries below are:
#   - boto3   → boto3     — the import name matches the pip name, but
#                          we list it explicitly as a smoke-test anchor
#                          for the #2773 regression that motivated this
#                          script. Removing this line is a signal the
#                          check has lost its original intent.
#   - psycopg → psycopg   — same as above; the daemon's canonical DB
#                          driver (#2729).
#   - yaml    → PyYAML    — classic import-vs-package mismatch. Not
#                          currently imported by the dispatcher, but
#                          listed as coverage for the import-vs-package
#                          mismatch case (#2776 acceptance criterion
#                          requires at least one such example).
# ---------------------------------------------------------------------------
IMPORT_TO_PACKAGE: dict[str, str] = {
    "boto3": "boto3",
    # ``botocore`` is a hard transitive dependency of ``boto3`` — pip
    # always installs it alongside ``boto3``, so mapping the import to
    # the ``boto3`` pip name keeps the check honest without requiring a
    # redundant ``botocore`` pip-install line in Dockerfile.dispatcher.
    # The dispatcher daemon imports ``botocore.config`` lazily inside
    # :meth:`DispatcherDaemon._make_ecs_client` to set explicit
    # connect/read timeouts (#3351).
    "botocore": "boto3",
    "psycopg": "psycopg",
    "yaml": "PyYAML",
}

# ---------------------------------------------------------------------------
# Imports that live inside scripts/dispatcher but are NOT external third-
# party packages — the dispatcher's own sibling modules. These never need
# a pip install entry because they are shipped via the Docker COPY layer.
#
# We match by top-level package name (``scripts`` is the only one, since
# dispatcher imports use ``from scripts.dispatcher.X import Y`` OR
# ``from .X import Y``). Relative imports are handled separately — see
# ``_collect_imports_from_file``.
# ---------------------------------------------------------------------------
INTERNAL_TOPLEVEL_PACKAGES: set[str] = {
    "scripts",
}

# Default location for the Dockerfile and dispatcher source dir. Override
# via CLI for tests that synthesize a temp tree.
DEFAULT_DOCKERFILE = Path("Dockerfile.dispatcher")
DEFAULT_SOURCE_DIR = Path("scripts/dispatcher")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Verify scripts/dispatcher/**/*.py imports are installed in "
            "Dockerfile.dispatcher."
        ),
    )
    parser.add_argument(
        "--dockerfile",
        type=Path,
        default=DEFAULT_DOCKERFILE,
        help=(
            "Path to the Dockerfile whose pip install step defines the "
            "installed package set. Default: Dockerfile.dispatcher."
        ),
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=(
            "Path to the dispatcher source directory. Only non-test *.py "
            "files directly inside this dir are scanned. "
            "Default: scripts/dispatcher."
        ),
    )
    return parser.parse_args(argv)


def _collect_imports_from_file(path: Path) -> set[str]:
    """Return the set of top-level package names imported by ``path``.

    Parses the file with ``ast.parse`` and walks every ``Import`` /
    ``ImportFrom`` node — including those nested inside function bodies,
    class bodies, and ``if``/``try`` blocks. This is how we catch lazy
    imports like the ``import boto3`` at the bottom of the daemon's
    ``_make_cloudwatch_client``.

    Relative imports (``from .X import Y``) are skipped — they target
    sibling files and are not third-party dependencies.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}") from exc

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise SystemExit(f"error: cannot parse {path}: {exc}") from exc

    top_level: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # ``import X.Y`` → top-level is ``X``.
                top_level.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # ``from .X import Y`` → relative; skip.
            if node.level and node.level > 0:
                continue
            if node.module is None:
                continue
            top_level.add(node.module.split(".")[0])
    return top_level


def _collect_imports_from_dir(source_dir: Path) -> dict[Path, set[str]]:
    """Collect imports from every non-test ``*.py`` file under ``source_dir``.

    Only the directory itself is scanned, not subdirectories named
    ``tests`` (test files may import pytest/mocks that are legitimately
    missing from the production image). ``__init__.py`` is included.
    """
    if not source_dir.is_dir():
        raise SystemExit(f"error: source-dir does not exist: {source_dir}")

    results: dict[Path, set[str]] = {}
    for py_file in sorted(source_dir.glob("*.py")):
        if py_file.name.startswith("test_"):
            continue
        results[py_file] = _collect_imports_from_file(py_file)
    return results


# ---------------------------------------------------------------------------
# Dockerfile parsing
# ---------------------------------------------------------------------------
# We look for ``pip install`` invocations inside RUN steps and extract
# the positional package arguments. A real-world example from
# Dockerfile.dispatcher:
#
#     RUN pip install --no-cache-dir \
#         "psycopg[binary]>=3.1,<4" \
#         "boto3>=1.34,<2"
#
# Goal: extract ``psycopg`` and ``boto3`` from this. We do NOT try to
# support every imaginable pip invocation (pip -r requirements.txt,
# pip install from a git URL, pip install -e .). The dispatcher image
# uses plain ``pip install "<spec>" ...`` and that is the narrow case we
# cover — per the issue's scope: "small explicit parser is sufficient
# for Dockerfile.dispatcher".
# ---------------------------------------------------------------------------

# Match a pip install command (``pip install`` or ``pip3 install``)
# inside a RUN line. Captures the remainder of the logical line (across
# backslash continuations) in group 1.
_PIP_INSTALL_RE = re.compile(r"\bpip3?\s+install\b(.*)", re.DOTALL)

# Flags that take an argument (so the next token is consumed as the
# flag's value, NOT as a package name). ``--no-cache-dir`` and friends
# do not take an argument and are stripped by the "starts with -" check
# below.
_PIP_FLAGS_WITH_VALUE = {
    "-c",
    "-e",
    "-i",
    "-r",
    "-t",
    "--constraint",
    "--extra-index-url",
    "--find-links",
    "--index-url",
    "--prefix",
    "--requirement",
    "--target",
}


def _join_continuations(text: str) -> str:
    """Collapse backslash-newline continuations into a single logical line.

    Dockerfile RUN steps commonly split pip install across lines with
    trailing backslashes — we treat the whole block as one line for
    parsing purposes.
    """
    # Also fold YAML-style pipe-continuations just in case (cheap and
    # harmless; no false positives on real Dockerfiles).
    return re.sub(r"\\\s*\n", " ", text)


def _normalize_package_spec(spec: str) -> str:
    """Reduce a pip spec to its package name, dropping extras and versions.

    Examples:
        ``"psycopg[binary]>=3.1,<4"`` → ``psycopg``
        ``"boto3>=1.34,<2"``          → ``boto3``
        ``"PyYAML"``                  → ``pyyaml``  (lowercased)
        ``"Flask==2.3.0"``            → ``flask``

    We lowercase to match PEP 503 normalized comparison — so ``PyYAML``
    and ``pyyaml`` compare equal. The ``IMPORT_TO_PACKAGE`` table is
    also lowercased on the value side before comparison.
    """
    # Strip surrounding quotes if present.
    spec = spec.strip().strip('"').strip("'")
    if not spec:
        return ""
    # Split on the first version-comparator or extras bracket.
    # Package name chars per PEP 508: [A-Za-z0-9._-]+
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", spec)
    if not match:
        return ""
    return match.group(0).lower()


def _parse_pip_tokens(args_str: str) -> list[str]:
    """Return positional package tokens from a pip install argument string.

    Skips flags (``--no-cache-dir``, ``--no-binary``, etc.) and
    flag+value pairs (``-r requirements.txt``).
    """
    # Tokenize on whitespace. Quoted specs (``"psycopg[binary]>=3.1,<4"``)
    # are preserved as single tokens because shlex-style quoting survives
    # the whitespace split when we treat the quote chars as part of the
    # token. Dockerfile-style quoting uses straight ASCII double quotes
    # for this purpose — we strip them in _normalize_package_spec.
    #
    # We use a simple shlex to correctly handle quoted tokens without
    # a full shell parse.
    import shlex

    try:
        tokens = shlex.split(args_str, posix=True)
    except ValueError:
        # Unclosed quote or similar; fall back to whitespace split.
        tokens = args_str.split()

    packages: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if not token:
            continue
        if token.startswith("-"):
            if token in _PIP_FLAGS_WITH_VALUE:
                skip_next = True
            # Anything else is a flag without an arg (``--no-cache-dir``,
            # ``--pre``) or an unknown flag; skip.
            continue
        packages.append(token)
    return packages


def _collect_installed_packages(dockerfile: Path) -> set[str]:
    """Return the normalized set of pip-installed package names.

    Scans ``dockerfile`` for every ``pip install`` occurrence and
    collects each positional argument as a package. Package names are
    lowercased per PEP 503 for comparison.
    """
    if not dockerfile.is_file():
        raise SystemExit(f"error: dockerfile does not exist: {dockerfile}")

    text = dockerfile.read_text(encoding="utf-8")
    text = _join_continuations(text)

    installed: set[str] = set()
    for line in text.splitlines():
        # Skip commented-out lines (Dockerfile comment syntax is `#`).
        # Lines of interest are RUN-style and contain ``pip install``.
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        match = _PIP_INSTALL_RE.search(line)
        if not match:
            continue
        tokens = _parse_pip_tokens(match.group(1))
        for tok in tokens:
            name = _normalize_package_spec(tok)
            if name:
                installed.add(name)
    return installed


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _build_report(
    *,
    missing: list[tuple[str, str, list[Path]]],
    dockerfile: Path,
) -> str:
    """Render a human-readable report listing missing packages.

    Each tuple is ``(import_name, pip_name, [files using it])``.
    """
    lines: list[str] = []
    lines.append(
        "ERROR: scripts/dispatcher imports packages that are not installed "
        f"in {dockerfile}."
    )
    lines.append("")
    lines.append(
        "  The dispatcher's production container (Fargate) will fail at runtime with"
    )
    lines.append("  'ModuleNotFoundError' when it executes these imports.")
    lines.append("")
    for import_name, pip_name, files in missing:
        file_list = ", ".join(sorted(str(f) for f in files))
        if import_name == pip_name:
            lines.append(
                f"    - import '{import_name}' — missing pip package '{pip_name}'"
            )
        else:
            lines.append(
                f"    - import '{import_name}' → pip package '{pip_name}' "
                f"— missing from Dockerfile"
            )
        lines.append(f"        used in: {file_list}")
    lines.append("")
    lines.append("  Fix: add the missing package(s) to the `pip install` step in")
    lines.append(f"  {dockerfile}. Pin a version floor that matches the rest of the")
    lines.append("  repo (e.g. 'boto3>=1.34,<2' per packages/*/pyproject.toml).")
    lines.append("")
    lines.append("  See issue #2776 for background and #2773 for the incident that")
    lines.append("  motivated this check.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Entry point. Returns process exit code."""
    args = _parse_args(argv)

    # 1. Gather imports.
    imports_by_file = _collect_imports_from_dir(args.source_dir)

    # Invert: import name -> set of files that use it.
    import_to_files: dict[str, set[Path]] = {}
    for path, imports in imports_by_file.items():
        for name in imports:
            import_to_files.setdefault(name, set()).add(path)

    # 2. Compute the set of dispatcher-sibling top-level modules. Any
    #    ``foo.py`` file directly inside ``source_dir`` exposes a
    #    top-level module name ``foo`` that another sibling can
    #    ``import foo`` (when invoked as a standalone script after a
    #    sys.path push) without needing a pip install. Scripts like
    #    ``ci_classifier_cli.py`` (#4417) use this pattern because
    #    they're invoked via ``python3 path/to/script.py`` rather than
    #    ``python -m scripts.dispatcher.script``.
    sibling_modules: set[str] = set()
    for entry in args.source_dir.glob("*.py"):
        if entry.name.startswith("test_"):
            continue
        sibling_modules.add(entry.stem)

    # 3. Filter out stdlib, internal, sibling, and __future__.
    stdlib = set(sys.stdlib_module_names)
    external_imports: dict[str, set[Path]] = {
        name: files
        for name, files in import_to_files.items()
        if name not in stdlib
        and name not in INTERNAL_TOPLEVEL_PACKAGES
        and name not in sibling_modules
        and name != "__future__"
    }

    # 3. Map imports to pip package names (lowercased for comparison).
    import_to_pip: dict[str, str] = {}
    for name in external_imports:
        pip_name = IMPORT_TO_PACKAGE.get(name, name)
        import_to_pip[name] = pip_name.lower()

    # 4. Parse the Dockerfile for installed packages.
    installed = _collect_installed_packages(args.dockerfile)

    # 5. Compute misses.
    missing: list[tuple[str, str, list[Path]]] = []
    for import_name, pip_name in sorted(import_to_pip.items()):
        if pip_name not in installed:
            missing.append(
                (
                    import_name,
                    # Preserve the canonical-case pip name from the table
                    # (or the import name if no mapping) for the report.
                    IMPORT_TO_PACKAGE.get(import_name, import_name),
                    sorted(external_imports[import_name]),
                )
            )

    if missing:
        print(
            _build_report(missing=missing, dockerfile=args.dockerfile),
            file=sys.stderr,
        )
        return 1

    # Success — brief summary so a passing run is visible in CI logs.
    external_names = ", ".join(sorted(external_imports.keys())) or "(none)"
    print(
        f"ok: all non-stdlib imports in {args.source_dir} are installed in "
        f"{args.dockerfile}."
    )
    print(f"    external imports: {external_names}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
