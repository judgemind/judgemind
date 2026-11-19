"""Which changed paths select a package's full test run.

One mapping shared by the pre-push hook (``.githooks/pre-push``) and
``/ralph`` (``scripts/ralph_touched_packages.py``), so a path cannot run a
package's tests in one and not the other (#5151). Before this module each
kept its own rules, and a ``pyproject.toml``-only change ran no tests in
either, though a dependency bump is the kind of edit that breaks them.

Package ids are ``<name>`` for ``packages/<name>/`` and ``infra/<dir>`` for
a Python package under ``infra/<dir>/``. The rules go by path alone, with
no filesystem access, so the same path always maps the same way:

Python (``packages/<name>/``):
  * ``.py`` files under ``src/`` or ``tests/``;
  * package-root files that shape the test run: ``conftest.py``,
    ``pyproject.toml``, ``setup.cfg``, ``setup.py``, ``pytest.ini``,
    ``tox.ini``, ``.coveragerc`` and ``requirements*.txt``.

Python (``infra/<dir>/``):
  * any ``.py`` file, plus the same package-root files.

TypeScript (``packages/<name>/``):
  * ``.ts`` / ``.tsx`` / ``.js`` / ``.jsx`` files under ``src/``, ``app/``,
    ``tests/``, ``__tests__/`` or ``lib/``;
  * package-root config: ``package.json``, ``package-lock.json``,
    ``tsconfig*.json`` and ``.ts`` / ``.tsx`` / ``.js`` / ``.mjs`` /
    ``.cjs`` files such as ``vitest.config.ts``.

Callers that know the filesystem narrow the result to real packages: the
hook keeps a Python id only when its ``pyproject.toml`` exists and a
TypeScript id only when its ``package.json`` exists.

CLI (used by the pre-push hook): reads repo-relative paths from stdin, one
per line, and prints one ``python<TAB><id>`` or ``ts<TAB><id>`` line per
selected package, sorted. Always exits 0; no output means no package
needs a test run.

Stdlib only, so it runs without a venv.
"""
# venv: none
# permanent: true

from __future__ import annotations

import sys

PY_ROOT_TEST_FILES = frozenset(
    {
        "conftest.py",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "pytest.ini",
        "tox.ini",
        ".coveragerc",
    }
)
PY_CODE_DIRS = ("src", "tests")

TS_ROOT_TEST_FILES = frozenset({"package.json", "package-lock.json"})
TS_ROOT_CONFIG_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".cjs")
TS_CODE_DIRS = ("src", "app", "tests", "__tests__", "lib")
TS_CODE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")


def split_package_path(path: str) -> tuple[str, str] | None:
    """Split *path* into ``(package_id, path_inside_package)``.

    Returns None for paths outside ``packages/<name>/`` and
    ``infra/<dir>/``, and for files directly under ``packages/`` or
    ``infra/``.
    """
    parts = path.split("/")
    if len(parts) < 3 or parts[0] not in ("packages", "infra") or not parts[1]:
        return None
    pkg_id = parts[1] if parts[0] == "packages" else f"infra/{parts[1]}"
    return pkg_id, "/".join(parts[2:])


def _is_py_root_test_file(name: str) -> bool:
    return name in PY_ROOT_TEST_FILES or (
        name.startswith("requirements") and name.endswith(".txt")
    )


def is_python_test_trigger(pkg_id: str, rel: str) -> bool:
    """True when *rel* (a path inside Python package *pkg_id*) selects its tests."""
    if "/" not in rel and _is_py_root_test_file(rel):
        return True
    if not rel.endswith(".py"):
        return False
    if pkg_id.startswith("infra/"):
        return True
    return rel.split("/", 1)[0] in PY_CODE_DIRS and "/" in rel


def is_ts_test_trigger(pkg_id: str, rel: str) -> bool:
    """True when *rel* (a path inside TypeScript package *pkg_id*) selects its tests."""
    if pkg_id.startswith("infra/"):
        return False
    if "/" not in rel:
        return (
            rel in TS_ROOT_TEST_FILES
            or (rel.startswith("tsconfig") and rel.endswith(".json"))
            or rel.endswith(TS_ROOT_CONFIG_SUFFIXES)
        )
    return rel.split("/", 1)[0] in TS_CODE_DIRS and rel.endswith(TS_CODE_SUFFIXES)


def select_test_packages(changed_files: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(python_ids, ts_ids)``: packages whose full test run *changed_files* select.

    Both lists are sorted and de-duplicated.
    """
    python_ids: set[str] = set()
    ts_ids: set[str] = set()
    for path in changed_files:
        split = split_package_path(path)
        if split is None:
            continue
        pkg_id, rel = split
        if is_python_test_trigger(pkg_id, rel):
            python_ids.add(pkg_id)
        if is_ts_test_trigger(pkg_id, rel):
            ts_ids.add(pkg_id)
    return sorted(python_ids), sorted(ts_ids)


def main() -> None:
    """CLI entry point: paths on stdin, ``<lang>\\t<id>`` lines on stdout."""
    paths = [line.strip() for line in sys.stdin if line.strip()]
    python_ids, ts_ids = select_test_packages(paths)
    for pkg_id in python_ids:
        print(f"python\t{pkg_id}")
    for pkg_id in ts_ids:
        print(f"ts\t{pkg_id}")


if __name__ == "__main__":
    main()
