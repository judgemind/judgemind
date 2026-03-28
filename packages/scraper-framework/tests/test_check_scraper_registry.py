"""Tests for the scraper registry consistency check script."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "check_scraper_registry.py"


def _load_script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_scraper_registry", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    courts_dir = repo_root / "packages" / "scraper-framework" / "src" / "courts" / "ca"
    runner_path = repo_root / "packages" / "scraper-framework" / "src" / "framework" / "runner.py"
    runner_path.parent.mkdir(parents=True, exist_ok=True)
    return repo_root, courts_dir


def test_discovers_only_tentative_scrapers_with_default_config(tmp_path: Path) -> None:
    module = _load_script_module()
    _, courts_dir = _make_repo(tmp_path)

    _write_file(
        courts_dir / "alpha_tentatives.py",
        """
from framework import ScraperConfig

class AlphaScraper(BaseScraper):
    pass

def default_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(scraper_id="alpha-id")
""".strip(),
    )
    _write_file(
        courts_dir / "beta_tentatives.py",
        """
from framework import ScraperConfig

class Helper:
    pass

def default_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(scraper_id="beta-id")
""".strip(),
    )
    _write_file(
        courts_dir / "gamma_tentatives.py",
        """
class GammaScraper(PdfLinkScraper):
    pass
""".strip(),
    )
    _write_file(
        courts_dir / "delta_calendar.py",
        """
from framework import ScraperConfig

class DeltaScraper(BaseScraper):
    pass

def default_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(scraper_id="delta-id")
""".strip(),
    )

    discovered = module.discover_expected_scrapers(courts_dir)

    assert [scraper.scraper_id for scraper in discovered] == ["alpha-id"]


def test_find_unregistered_scrapers_returns_missing_entries(tmp_path: Path) -> None:
    module = _load_script_module()
    repo_root, courts_dir = _make_repo(tmp_path)
    runner_path = repo_root / "packages" / "scraper-framework" / "src" / "framework" / "runner.py"

    _write_file(
        courts_dir / "alpha_tentatives.py",
        """
from framework import ScraperConfig

class AlphaScraper(BaseScraper):
    pass

def default_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(scraper_id="alpha-id")
""".strip(),
    )
    _write_file(
        courts_dir / "beta_tentatives.py",
        """
from framework import ScraperConfig

class BetaScraper(PdfLinkScraper):
    pass

def default_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(scraper_id="beta-id")
""".strip(),
    )
    _write_file(
        runner_path,
        """
_REGISTRY = []

def _build_registry():
    _REGISTRY.extend(
        [
            ("alpha-id", AlphaScraper, alpha_config),
        ]
    )
    return _REGISTRY
""".strip(),
    )

    missing = module.find_unregistered_scrapers(courts_dir, runner_path)

    assert [(scraper.scraper_id, scraper.module_name) for scraper in missing] == [
        ("beta-id", "beta_tentatives")
    ]


def test_cli_exits_nonzero_and_lists_missing_scrapers(tmp_path: Path) -> None:
    repo_root, courts_dir = _make_repo(tmp_path)
    runner_path = repo_root / "packages" / "scraper-framework" / "src" / "framework" / "runner.py"

    _write_file(
        courts_dir / "alpha_tentatives.py",
        """
from framework import ScraperConfig

class AlphaScraper(BaseScraper):
    pass

def default_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(scraper_id="alpha-id")
""".strip(),
    )
    _write_file(
        runner_path,
        """
_REGISTRY = []

def _build_registry():
    _REGISTRY.extend([])
    return _REGISTRY
""".strip(),
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "alpha-id" in result.stdout
    assert "alpha_tentatives.py" in result.stdout


def test_cli_exits_zero_when_all_scrapers_are_registered(tmp_path: Path) -> None:
    repo_root, courts_dir = _make_repo(tmp_path)
    runner_path = repo_root / "packages" / "scraper-framework" / "src" / "framework" / "runner.py"

    _write_file(
        courts_dir / "alpha_tentatives.py",
        """
from framework import ScraperConfig

class AlphaScraper(BaseScraper):
    pass

def default_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(scraper_id="alpha-id")
""".strip(),
    )
    _write_file(
        runner_path,
        """
_REGISTRY = []

def _build_registry():
    _REGISTRY.extend(
        [
            ("alpha-id", AlphaScraper, alpha_config),
        ]
    )
    return _REGISTRY
""".strip(),
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "All scraper modules are registered" in result.stdout
