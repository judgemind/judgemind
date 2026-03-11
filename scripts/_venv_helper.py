"""Shared venv auto-detection for scripts that depend on project packages.

Scripts that need non-stdlib dependencies should call ``ensure_venv()`` at
the top of the file, before any non-stdlib imports.  If the script is not
already running inside the target venv, it will re-exec itself with the
correct Python interpreter via ``os.execv``.

Usage example (for a script that needs packages/telegram-bridge)::

    # At the top of the script, before non-stdlib imports:
    from _venv_helper import ensure_venv
    ensure_venv("telegram-bridge")

    # Now safe to import non-stdlib modules
    import boto3
    ...

For scripts that need packages/scraper-framework::

    from _venv_helper import ensure_venv
    ensure_venv("scraper-framework")

If the venv does not exist, the script exits with a clear error message
telling the user how to create it.

Environment variables:
    _VENV_HELPER_SKIP: Set to any non-empty value to skip the venv check.
        Useful in test environments or containers where dependencies are
        already available without a venv.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Resolve repo root: scripts/ is a direct child of the repo root.
# For worktrees, resolve through symlinks to find the actual directory.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent


def _find_repo_root() -> Path:
    """Return the repo root (parent of scripts/).

    Works correctly in both the main repo and worktrees because
    ``__file__`` is resolved through symlinks.
    """
    return _REPO_ROOT


def _venv_python(package_name: str) -> Path:
    """Return the expected venv Python path for a given package."""
    return _find_repo_root() / "packages" / package_name / ".venv" / "bin" / "python3"


def _check_venv_has_deps(package_name: str) -> bool:
    """Check whether the venv for *package_name* has dependencies installed.

    Looks for at least one ``.dist-info`` directory in the venv's
    site-packages (beyond pip/setuptools) as a proxy for "deps installed".
    Returns ``True`` if the venv appears to have dependencies, ``False``
    if it looks empty or has only bootstrapping packages.
    """
    venv_py = _venv_python(package_name)
    site_packages = venv_py.parent.parent / "lib"

    # Find the site-packages directory (e.g. lib/python3.12/site-packages/).
    # There should be exactly one pythonX.Y directory.
    if not site_packages.exists():
        return False
    for child in site_packages.iterdir():
        sp = child / "site-packages"
        if sp.is_dir():
            # Count dist-info dirs that aren't pip/setuptools/wheel.
            bootstrap_prefixes = ("pip-", "setuptools-", "wheel-", "_distutils_hack")
            non_bootstrap = [
                d
                for d in sp.iterdir()
                if d.name.endswith(".dist-info")
                and not any(d.name.startswith(p) for p in bootstrap_prefixes)
            ]
            return len(non_bootstrap) > 0
    return False


def _print_install_instructions(package_name: str) -> None:
    """Print clear instructions for installing deps into the venv."""
    repo_root = _find_repo_root()
    pkg_dir = repo_root / "packages" / package_name
    venv_py = _venv_python(package_name)
    print(
        f"ERROR: venv at {venv_py.parent.parent} exists but has no "
        f"dependencies installed.\n"
        f"\n"
        f"This script requires the '{package_name}' package and its deps.\n"
        f"Install them with:\n"
        f"\n"
        f"    {pkg_dir}/.venv/bin/pip install -e \"{pkg_dir}[dev]\" --quiet\n",
        file=sys.stderr,
    )


def ensure_venv(package_name: str) -> None:
    """Ensure the script is running inside the venv for *package_name*.

    If the current Python interpreter is not the venv's Python, re-exec
    the script with the correct interpreter (preserving all arguments).
    If the venv does not exist, print a helpful error and exit.
    If the venv exists but has no dependencies installed, print a helpful
    error and exit instead of crashing with a raw ``ModuleNotFoundError``.

    This function only returns if:
    - We are already in the correct venv, or
    - ``_VENV_HELPER_SKIP`` env var is set (for tests/containers).

    Otherwise it calls ``os.execv`` (which replaces the process) or
    ``sys.exit`` (if the venv is missing or empty).

    Args:
        package_name: The package directory name under ``packages/``
            (e.g. ``"scraper-framework"``, ``"telegram-bridge"``).
    """
    # Allow skipping in test environments or containers where deps
    # are already available without a venv.
    if os.environ.get("_VENV_HELPER_SKIP"):
        return

    venv_py = _venv_python(package_name)

    # Already running in the correct venv?
    try:
        if os.path.realpath(sys.executable) == os.path.realpath(str(venv_py)):
            # We are in the right venv — verify deps are installed.
            if not _check_venv_has_deps(package_name):
                _print_install_instructions(package_name)
                sys.exit(1)
            return
    except OSError:
        pass  # venv_py might not exist yet — fall through

    # Check if the venv exists
    if not venv_py.exists():
        repo_root = _find_repo_root()
        pkg_dir = repo_root / "packages" / package_name
        print(
            f"ERROR: venv not found at {venv_py}\n"
            f"\n"
            f"This script requires the '{package_name}' package venv.\n"
            f"Create it with:\n"
            f"\n"
            f"    python3.12 -m venv {pkg_dir}/.venv\n"
            f"    {pkg_dir}/.venv/bin/pip install -e \"{pkg_dir}[dev]\" --quiet\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Venv exists but may be empty — check before re-exec.
    if not _check_venv_has_deps(package_name):
        _print_install_instructions(package_name)
        sys.exit(1)

    # Re-exec with the venv Python
    os.execv(str(venv_py), [str(venv_py), *sys.argv])
