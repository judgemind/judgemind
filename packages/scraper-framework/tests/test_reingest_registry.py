"""Regression tests for the reingest scraper registry.

Verifies that _load_scraper_registry() in reingest_from_s3.py correctly maps
every known scraper module to its canonical scraper_id. This would have caught
the original bug where import names and scraper_id keys were wrong for every
non-LA scraper (see #358).
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Import the reingest module from scripts/
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
reingest = importlib.import_module("reingest_from_s3")

# ---------------------------------------------------------------------------
# Enumerate all known scraper modules and their default_config().scraper_id
# ---------------------------------------------------------------------------

# Each tuple: (module_path relative to courts package, class name)
_SCRAPER_MODULES: list[tuple[str, str]] = [
    ("courts.ca.la_tentatives", "LATentativeRulingsScraper"),
    ("courts.ca.oc_tentatives", "OCTentativeRulingsScraper"),
    ("courts.ca.oc_family_law_tentatives", "OCFamilyLawTentativeRulingsScraper"),
    ("courts.ca.oc_probate_tentatives", "OCProbateTentativeRulingsScraper"),
    ("courts.ca.sb_tentatives", "SBTentativeRulingsScraper"),
    ("courts.ca.sf_tentatives", "SFTentativeRulingsScraper"),
    ("courts.ca.sc_tentatives", "SCTentativeRulingsScraper"),
    ("courts.ca.riverside_tentatives", "RiversideTentativeRulingsScraper"),
    ("courts.ca.fresno_tentatives", "FresnoTentativeRulingsScraper"),
]


def _load_module(module_path: str) -> ModuleType:
    """Import a scraper module by its dotted path."""
    return importlib.import_module(module_path)


def _get_all_scraper_ids() -> dict[str, str]:
    """Return {scraper_id: module_path} for every known scraper module."""
    result: dict[str, str] = {}
    for module_path, _class_name in _SCRAPER_MODULES:
        mod = _load_module(module_path)
        config = mod.default_config()
        result[config.scraper_id] = module_path
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScraperRegistryNonEmpty:
    """The registry must load successfully and contain entries."""

    def test_registry_is_non_empty(self) -> None:
        """_load_scraper_registry() must populate at least one entry."""
        # Clear any cached state
        reingest._SCRAPER_REGISTRY.clear()
        reingest._load_scraper_registry()
        assert len(reingest._SCRAPER_REGISTRY) > 0, (
            "_SCRAPER_REGISTRY is empty after _load_scraper_registry() — "
            "all imports likely failed silently"
        )

    def test_registry_has_all_known_scrapers(self) -> None:
        """The registry must contain an entry for every known scraper module."""
        reingest._SCRAPER_REGISTRY.clear()
        reingest._load_scraper_registry()
        assert len(reingest._SCRAPER_REGISTRY) == len(_SCRAPER_MODULES), (
            f"Registry has {len(reingest._SCRAPER_REGISTRY)} entries but "
            f"there are {len(_SCRAPER_MODULES)} known scraper modules"
        )


class TestRegistryKeysMatchDefaultConfig:
    """Every registered key must match the scraper_id from default_config()."""

    def test_all_keys_match_default_config_scraper_id(self) -> None:
        """Each key in the registry must equal the class's default_config().scraper_id."""
        reingest._SCRAPER_REGISTRY.clear()
        reingest._load_scraper_registry()

        for registry_key, scraper_cls in reingest._SCRAPER_REGISTRY.items():
            # Find which module this class comes from
            module_name = scraper_cls.__module__
            mod = importlib.import_module(module_name)
            config = mod.default_config()
            assert registry_key == config.scraper_id, (
                f"Registry key {registry_key!r} does not match "
                f"default_config().scraper_id {config.scraper_id!r} "
                f"from {module_name}"
            )


class TestRegistryCoverage:
    """Every scraper module's default_config().scraper_id must be in the registry."""

    def test_every_scraper_module_covered(self) -> None:
        """No scraper module should be missing from the registry."""
        reingest._SCRAPER_REGISTRY.clear()
        reingest._load_scraper_registry()

        all_scraper_ids = _get_all_scraper_ids()
        registry_keys = set(reingest._SCRAPER_REGISTRY.keys())

        missing = set(all_scraper_ids.keys()) - registry_keys
        assert not missing, (
            f"Scraper IDs missing from registry: {missing}. "
            f"Modules: {[all_scraper_ids[sid] for sid in missing]}"
        )

        extra = registry_keys - set(all_scraper_ids.keys())
        assert not extra, (
            f"Registry contains unknown scraper IDs: {extra}. "
            f"These don't match any known scraper module's default_config().scraper_id"
        )


class TestRegistryClassesInstantiable:
    """Every scraper class in the registry must be instantiable."""

    @pytest.mark.parametrize(
        "module_path,class_name",
        _SCRAPER_MODULES,
        ids=[m for m, _c in _SCRAPER_MODULES],
    )
    def test_scraper_class_instantiable(self, module_path: str, class_name: str) -> None:
        """Each registered scraper class can be instantiated with its default config."""
        mod = _load_module(module_path)
        config = mod.default_config()
        scraper_cls = getattr(mod, class_name)
        scraper = scraper_cls(config=config)
        assert scraper is not None
        assert scraper.config.scraper_id == config.scraper_id
