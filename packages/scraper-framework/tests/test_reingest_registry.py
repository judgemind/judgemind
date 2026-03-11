"""Regression tests for the reingest scraper registry.

Verifies that _load_scraper_registry() in reingest_from_s3.py correctly maps
every known scraper module to its canonical scraper_id. This would have caught
the original bug where import names and scraper_id keys were wrong for every
non-LA scraper (see #358).

Scraper modules are auto-discovered rather than hardcoded so that adding a new
scraper never requires updating this test file (see #680).
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import sys
from types import ModuleType

import pytest

from framework.base import BaseScraper

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
# Auto-discover all scraper modules that expose default_config()
# ---------------------------------------------------------------------------


def _discover_scraper_modules() -> list[tuple[str, str]]:
    """Return [(module_path, class_name)] for every scraper with default_config().

    Mirrors the discovery logic in ``_load_scraper_registry()`` — walks the
    ``courts`` package tree and collects modules that expose a
    ``default_config()`` callable and a concrete ``BaseScraper`` subclass.
    """
    import courts

    result: list[tuple[str, str]] = []

    for _importer, modname, ispkg in pkgutil.walk_packages(courts.__path__, prefix="courts."):
        if ispkg:
            continue
        try:
            mod = importlib.import_module(modname)
        except Exception:  # noqa: BLE001
            continue

        config_fn = getattr(mod, "default_config", None)
        if config_fn is None or not callable(config_fn):
            continue

        # Find the concrete BaseScraper subclass defined in this module.
        scraper_cls_name: str | None = None
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, BaseScraper)
                and obj is not BaseScraper
                and obj.__module__ == mod.__name__
            ):
                scraper_cls_name = name
                break

        if scraper_cls_name is None:
            continue

        result.append((modname, scraper_cls_name))

    return sorted(result)


_SCRAPER_MODULES: list[tuple[str, str]] = _discover_scraper_modules()


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
        """The registry must contain an entry for every discovered scraper module."""
        reingest._SCRAPER_REGISTRY.clear()
        reingest._load_scraper_registry()
        assert len(reingest._SCRAPER_REGISTRY) == len(_SCRAPER_MODULES), (
            f"Registry has {len(reingest._SCRAPER_REGISTRY)} entries but "
            f"auto-discovery found {len(_SCRAPER_MODULES)} scraper modules"
        )

    def test_discovery_found_scrapers(self) -> None:
        """Auto-discovery must find at least one scraper module."""
        assert len(_SCRAPER_MODULES) > 0, (
            "Auto-discovery found zero scraper modules — "
            "check that the courts package is importable"
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
