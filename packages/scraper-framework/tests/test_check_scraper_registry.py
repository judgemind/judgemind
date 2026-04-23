"""Tests for scripts/check-scraper-registry.py.

Validates that the scraper registry check script:
1. Correctly identifies all scraper modules with ``default_config``
2. Correctly parses registered modules from ``runner.py``
3. Detects unregistered scrapers (simulated via patching)
4. Passes on the current codebase (all scrapers registered)

See https://github.com/judgemind/judgemind/issues/2132 for context.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts/ to sys.path so we can import the check module.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

# ruff: noqa: E402
# Import the module under a clean name to avoid conflicts
import importlib

check_registry = importlib.import_module("check-scraper-registry")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

COURTS_DIR = _REPO_ROOT / "packages" / "scraper-framework" / "src" / "courts"
RUNNER_PATH = _REPO_ROOT / "packages" / "scraper-framework" / "src" / "framework" / "runner.py"


# ---------------------------------------------------------------------------
# Tests for find_scraper_modules
# ---------------------------------------------------------------------------


class TestFindScraperModules:
    """Tests for the find_scraper_modules function."""

    def test_finds_all_known_scraper_modules(self) -> None:
        """Every known scraper module with default_config is discovered."""
        modules = check_registry.find_scraper_modules(COURTS_DIR)
        # These are the modules we know have default_config
        expected_modules = [
            "courts.ca.cc_tentatives",
            "courts.ca.fresno_tentatives",
            "courts.ca.governor_appointments",
            "courts.ca.la_tentatives",
            "courts.ca.oc_family_law_tentatives",
            "courts.ca.oc_probate_tentatives",
            "courts.ca.oc_tentatives",
            "courts.ca.riverside_tentatives",
            "courts.ca.sb_tentatives",
            "courts.ca.sc_tentatives",
            "courts.ca.sd_calendar",
            "courts.ca.sd_pipeline",
            "courts.ca.sd_tentatives",
            "courts.ca.sf_tentatives",
            "courts.ca.ventura_tentatives",
            "courts.federal.courtlistener",
        ]
        for mod in expected_modules:
            assert mod in modules, f"Expected module {mod} not found"

    def test_excludes_helper_modules(self) -> None:
        """Helper modules without default_config are not included."""
        modules = check_registry.find_scraper_modules(COURTS_DIR)
        excluded = [
            "courts.ca.example",
            "courts.ca.pdf_link_scraper",
            "courts.ca.la_dept_judges",
            "courts.ca.riverside_dept_judges",
            "courts.ca.ventura_dept_judges",
            "courts.ca.la_title_utils",
        ]
        for mod in excluded:
            assert mod not in modules, f"Helper module {mod} should not be included"

    def test_excludes_init_files(self) -> None:
        """__init__.py files are not included."""
        modules = check_registry.find_scraper_modules(COURTS_DIR)
        for mod in modules:
            assert not mod.endswith("__init__"), f"__init__ module should not be included: {mod}"

    def test_returns_sorted_list(self) -> None:
        """Modules are returned in sorted order."""
        modules = check_registry.find_scraper_modules(COURTS_DIR)
        assert modules == sorted(modules)

    def test_discovers_modules_in_subdirectories(self) -> None:
        """Modules in subdirectories (e.g. courts/federal/) are discovered."""
        modules = check_registry.find_scraper_modules(COURTS_DIR)
        federal_modules = [m for m in modules if m.startswith("courts.federal.")]
        assert len(federal_modules) >= 1, "Should find at least one federal module"
        assert "courts.federal.courtlistener" in federal_modules

    def test_with_temp_directory(self, tmp_path: Path) -> None:
        """Discover modules from a temporary directory structure."""
        # Create a fake courts directory
        ca_dir = tmp_path / "courts" / "ca"
        ca_dir.mkdir(parents=True)
        (ca_dir / "__init__.py").write_text("")

        # Module with default_config
        (ca_dir / "foo_tentatives.py").write_text(
            textwrap.dedent("""\
                from __future__ import annotations

                def default_config(s3_bucket: str = "") -> object:
                    return None
            """)
        )

        # Helper module without default_config
        (ca_dir / "foo_helpers.py").write_text(
            textwrap.dedent("""\
                def some_helper() -> None:
                    pass
            """)
        )

        courts_dir = tmp_path / "courts"
        modules = check_registry.find_scraper_modules(courts_dir)
        assert modules == ["courts.ca.foo_tentatives"]


# ---------------------------------------------------------------------------
# Tests for find_registered_modules
# ---------------------------------------------------------------------------


class TestFindRegisteredModules:
    """Tests for the find_registered_modules function."""

    def test_finds_all_registered_modules_in_runner(self) -> None:
        """All expected modules are found in the real runner.py."""
        registered = check_registry.find_registered_modules(RUNNER_PATH)
        expected = {
            "courts.ca.cc_tentatives",
            "courts.ca.fresno_tentatives",
            "courts.ca.governor_appointments",
            "courts.ca.la_tentatives",
            "courts.ca.oc_family_law_tentatives",
            "courts.ca.oc_probate_tentatives",
            "courts.ca.oc_tentatives",
            "courts.ca.riverside_tentatives",
            "courts.ca.sb_tentatives",
            "courts.ca.sc_tentatives",
            "courts.ca.sd_calendar",
            "courts.ca.sd_pipeline",
            "courts.ca.sd_tentatives",
            "courts.ca.sf_tentatives",
            "courts.ca.ventura_tentatives",
            "courts.federal.courtlistener",
        }
        for mod in expected:
            assert mod in registered, f"Expected {mod} to be registered in runner.py"

    def test_with_synthetic_runner(self, tmp_path: Path) -> None:
        """Parses imports from a synthetic runner.py file."""
        runner = tmp_path / "runner.py"
        runner.write_text(
            textwrap.dedent("""\
                _REGISTRY = []

                def _build_registry():
                    if _REGISTRY:
                        return _REGISTRY

                    from courts.ca.foo_scraper import FooScraper
                    from courts.ca.foo_scraper import default_config as foo_config
                    from courts.ca.bar_scraper import BarScraper
                    from courts.ca.bar_scraper import default_config as bar_config

                    _REGISTRY.extend([
                        ("ca-foo", FooScraper, foo_config),
                        ("ca-bar", BarScraper, bar_config),
                    ])
                    return _REGISTRY

                def other_function():
                    pass
            """)
        )
        registered = check_registry.find_registered_modules(runner)
        assert registered == {"courts.ca.foo_scraper", "courts.ca.bar_scraper"}

    def test_ignores_imports_outside_build_registry(self, tmp_path: Path) -> None:
        """Imports outside _build_registry are not included."""
        runner = tmp_path / "runner.py"
        runner.write_text(
            textwrap.dedent("""\
                from courts.ca.outside_module import OutsideScraper

                _REGISTRY = []

                def _build_registry():
                    if _REGISTRY:
                        return _REGISTRY

                    from courts.ca.inside_module import InsideScraper
                    from courts.ca.inside_module import default_config as inside_config

                    _REGISTRY.extend([
                        ("ca-inside", InsideScraper, inside_config),
                    ])
                    return _REGISTRY
            """)
        )
        registered = check_registry.find_registered_modules(runner)
        assert "courts.ca.inside_module" in registered
        assert "courts.ca.outside_module" not in registered


# ---------------------------------------------------------------------------
# Tests for main (end-to-end)
# ---------------------------------------------------------------------------


class TestMain:
    """End-to-end tests for the main function."""

    def test_passes_with_current_codebase(self) -> None:
        """The check passes on the current codebase (all scrapers registered)."""
        exit_code = check_registry.main()
        assert exit_code == 0

    def test_detects_missing_module(self, tmp_path: Path) -> None:
        """Exits 1 when a module with default_config is not in runner.py."""
        # Create the full expected directory structure:
        # tmp_path/packages/scraper-framework/src/courts/ca/
        courts_dir = tmp_path / "packages" / "scraper-framework" / "src" / "courts" / "ca"
        courts_dir.mkdir(parents=True)
        (courts_dir / "__init__.py").write_text("")
        # Also need courts/__init__.py
        (courts_dir.parent / "__init__.py").write_text("")

        # Registered module
        (courts_dir / "registered.py").write_text(
            'def default_config(s3_bucket: str = "") -> object:\n    return None\n'
        )
        # Unregistered module
        (courts_dir / "unregistered.py").write_text(
            'def default_config(s3_bucket: str = "") -> object:\n    return None\n'
        )

        # Create a runner that only imports the registered module
        fw_dir = tmp_path / "packages" / "scraper-framework" / "src" / "framework"
        fw_dir.mkdir(parents=True)
        runner = fw_dir / "runner.py"
        runner.write_text(
            textwrap.dedent("""\
                _REGISTRY = []

                def _build_registry():
                    if _REGISTRY:
                        return _REGISTRY

                    from courts.ca.registered import RegisteredScraper
                    from courts.ca.registered import default_config as reg_config

                    _REGISTRY.extend([
                        ("ca-registered", RegisteredScraper, reg_config),
                    ])
                    return _REGISTRY
            """)
        )

        # Patch the repo root to use our temp dir
        with (
            patch.object(check_registry, "find_repo_root", return_value=tmp_path),
        ):
            old_argv = sys.argv
            sys.argv = ["check-scraper-registry.py"]
            try:
                exit_code = check_registry.main()
            finally:
                sys.argv = old_argv

        assert exit_code == 1

    def test_verbose_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verbose mode lists all modules with their status."""
        old_argv = sys.argv
        sys.argv = ["check-scraper-registry.py", "--verbose"]
        try:
            check_registry.main()
        finally:
            sys.argv = old_argv

        captured = capsys.readouterr()
        assert "REGISTERED:" in captured.out
        assert "courts.ca.cc_tentatives" in captured.out
