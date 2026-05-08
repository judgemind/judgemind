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
    """Return [(module_path, class_name)] for every (scraper_id, class) pair.

    Mirrors the discovery logic in ``_load_scraper_registry()`` — walks the
    ``courts`` package tree and collects modules that expose a
    ``default_config()`` callable and a concrete ``BaseScraper`` subclass.

    Multi-scraper modules (those exporting ``_SCRAPER_CLASS_BY_ID``) emit
    one tuple per registered scraper_id (#2599).
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

        # Multi-scraper opt-in: module-level ``_SCRAPER_CLASS_BY_ID`` maps
        # each scraper_id to its concrete class.  Each entry produces one
        # (module_path, class_name) tuple.
        scraper_class_by_id: dict[str, type] | None = getattr(mod, "_SCRAPER_CLASS_BY_ID", None)
        if scraper_class_by_id:
            for _sid, cls in scraper_class_by_id.items():
                result.append((modname, cls.__name__))
            continue

        # Fallback: single-class auto-detect (backward compat).
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


def _discover_config_factories(mod: ModuleType) -> list:
    """Return all ``default_config`` / ``default_config_<suffix>`` callables.

    Multi-scraper modules (#2599) expose multiple factories; single-scraper
    modules expose just ``default_config``.
    """
    factories = []
    for name, obj in inspect.getmembers(mod, inspect.isfunction):
        if (name == "default_config" or name.startswith("default_config_")) and (
            obj.__module__ == mod.__name__
        ):
            factories.append(obj)
    return factories


def _get_all_scraper_ids() -> dict[str, str]:
    """Return {scraper_id: module_path} for every known scraper module.

    Multi-scraper modules contribute one entry per ``default_config*``
    factory (#2599).  Modules that export ``_SPLIT_REGISTRY_ALIASES`` also
    contribute one entry per alias (#4386 — the rebuild-path scraper_id
    must resolve to the same scraper class as the canonical id so
    ``_reparse_document`` can call ``parse_document`` on rebuild rows).
    """
    result: dict[str, str] = {}
    seen_modules: set[str] = set()
    for module_path, _class_name in _SCRAPER_MODULES:
        if module_path in seen_modules:
            continue
        seen_modules.add(module_path)
        mod = _load_module(module_path)
        for factory in _discover_config_factories(mod):
            config = factory()
            result[config.scraper_id] = module_path
        # Include _SPLIT_REGISTRY_ALIASES entries — these are registered
        # in ``_SCRAPER_REGISTRY`` by the loader (#4386), so they must
        # appear in the expected set or the coverage check below would
        # flag them as "extra unknown ids".
        for alias in getattr(mod, "_SPLIT_REGISTRY_ALIASES", []) or []:
            result[alias] = module_path
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
        """The registry must contain an entry for every discovered scraper module.

        Counts both canonical scraper_ids (one per registered class) and
        ``_SPLIT_REGISTRY_ALIASES`` entries (one per alias per multi-class
        module — both LA classes share the same alias, so the alias adds
        one entry total even though the module has two classes).
        """
        reingest._SCRAPER_REGISTRY.clear()
        reingest._load_scraper_registry()
        # Canonical entries: one per (module, class) tuple in _SCRAPER_MODULES.
        canonical_count = len(_SCRAPER_MODULES)
        # Alias entries: each module's _SPLIT_REGISTRY_ALIASES register the
        # *same* alias key across all its classes, so the alias contributes
        # only one entry per module per alias (the second class's
        # ``_SCRAPER_REGISTRY[alias] = scraper_cls`` overwrites the first
        # under the same key).
        alias_count = 0
        seen_modules: set[str] = set()
        for module_path, _class_name in _SCRAPER_MODULES:
            if module_path in seen_modules:
                continue
            seen_modules.add(module_path)
            mod = _load_module(module_path)
            alias_count += len(getattr(mod, "_SPLIT_REGISTRY_ALIASES", []) or [])
        expected_count = canonical_count + alias_count
        assert len(reingest._SCRAPER_REGISTRY) == expected_count, (
            f"Registry has {len(reingest._SCRAPER_REGISTRY)} entries but "
            f"auto-discovery found {canonical_count} scraper modules and "
            f"{alias_count} alias entries (total expected {expected_count})"
        )

    def test_discovery_found_scrapers(self) -> None:
        """Auto-discovery must find at least one scraper module."""
        assert len(_SCRAPER_MODULES) > 0, (
            "Auto-discovery found zero scraper modules — "
            "check that the courts package is importable"
        )


class TestRegistryKeysMatchDefaultConfig:
    """Every registered key must match a ``default_config*`` factory's scraper_id."""

    def test_all_keys_match_default_config_scraper_id(self) -> None:
        """Each key must equal a ``default_config*()`` factory's scraper_id or a declared alias.

        Aliases come from a module's ``_SPLIT_REGISTRY_ALIASES`` and resolve
        the rebuild-path scraper_ids emitted by ``scripts/rebuild_db.py``
        to the canonical scraper class (#4386).  An alias entry is valid
        iff the alias appears in the same module's ``_SPLIT_REGISTRY_ALIASES``
        as the registered class — guards against accidental cross-module
        alias bleeding.
        """
        reingest._SCRAPER_REGISTRY.clear()
        reingest._load_scraper_registry()

        for registry_key, scraper_cls in reingest._SCRAPER_REGISTRY.items():
            # Find which module this class comes from.
            module_name = scraper_cls.__module__
            mod = importlib.import_module(module_name)

            # Collect all scraper_ids this module's factories produce —
            # for single-scraper modules this is one id; for multi-scraper
            # modules (#2599) it's one per factory.
            factory_ids = {f().scraper_id for f in _discover_config_factories(mod)}
            # Alias scraper_ids declared by this module (#4386).
            alias_ids = set(getattr(mod, "_SPLIT_REGISTRY_ALIASES", []) or [])

            assert registry_key in (factory_ids | alias_ids), (
                f"Registry key {registry_key!r} does not match any "
                f"default_config*().scraper_id or _SPLIT_REGISTRY_ALIASES "
                f"in {module_name} (module offers {sorted(factory_ids)}, "
                f"aliases {sorted(alias_ids)})"
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


def _instantiable_params() -> list[tuple[str, str]]:
    """Return (module_path, class_name) pairs with duplicates collapsed.

    Multi-scraper modules show up once per class in ``_SCRAPER_MODULES``
    but we only need to test one instantiation per class.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for m, c in _SCRAPER_MODULES:
        if (m, c) in seen:
            continue
        seen.add((m, c))
        out.append((m, c))
    return out


_INSTANTIABLE_PARAMS = _instantiable_params()


class TestRegistryClassesInstantiable:
    """Every scraper class in the registry must be instantiable."""

    @pytest.mark.parametrize(
        "module_path,class_name",
        _INSTANTIABLE_PARAMS,
        ids=[f"{m}::{c}" for m, c in _INSTANTIABLE_PARAMS],
    )
    def test_scraper_class_instantiable(self, module_path: str, class_name: str) -> None:
        """Each registered scraper class can be instantiated with a default config.

        For multi-scraper modules (#2599), each class is instantiated with
        whichever ``default_config*()`` factory returns the scraper_id for
        this class (looked up via ``_SCRAPER_CLASS_BY_ID``).
        """
        mod = _load_module(module_path)
        scraper_cls = getattr(mod, class_name)

        # Resolve the matching factory for this class.
        scraper_class_by_id: dict[str, type] | None = getattr(mod, "_SCRAPER_CLASS_BY_ID", None)
        if scraper_class_by_id:
            scraper_id = next(sid for sid, cls in scraper_class_by_id.items() if cls is scraper_cls)
            # Find the factory that returns this scraper_id.
            config = None
            for factory in _discover_config_factories(mod):
                candidate = factory()
                if candidate.scraper_id == scraper_id:
                    config = candidate
                    break
            assert config is not None, (
                f"No default_config*() factory in {module_path} returns scraper_id {scraper_id!r}"
            )
        else:
            config = mod.default_config()

        scraper = scraper_cls(config=config)
        assert scraper is not None
        assert scraper.config.scraper_id == config.scraper_id


class TestLlmSplitRegistryDiscovery:
    """Verify that _load_scraper_registry() discovers _llm_extract_rulings functions (#1969)."""

    def test_llm_split_registry_populated(self) -> None:
        """Modules that export _llm_extract_rulings must be in _LLM_SPLIT_REGISTRY.

        Multi-scraper modules (#2599) register the LLM split function under
        every scraper_id the module exposes.
        """
        reingest._SCRAPER_REGISTRY.clear()
        reingest._LLM_SPLIT_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()
        reingest._load_scraper_registry()

        # Discover which scraper_ids live in modules that export
        # _llm_extract_rulings.
        expected_llm_ids: set[str] = set()
        seen_modules: set[str] = set()
        for module_path, _class_name in _SCRAPER_MODULES:
            if module_path in seen_modules:
                continue
            seen_modules.add(module_path)
            mod = _load_module(module_path)
            if hasattr(mod, "_llm_extract_rulings") and callable(mod._llm_extract_rulings):
                for factory in _discover_config_factories(mod):
                    expected_llm_ids.add(factory().scraper_id)

        assert expected_llm_ids, (
            "No scraper modules export _llm_extract_rulings — "
            "registry discovery is broken or no LLM scrapers registered"
        )

        actual_llm_ids = set(reingest._LLM_SPLIT_REGISTRY.keys())
        missing = expected_llm_ids - actual_llm_ids
        assert not missing, (
            f"Scraper IDs with _llm_extract_rulings not in _LLM_SPLIT_REGISTRY: {missing}"
        )
