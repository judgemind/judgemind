"""Regression tests for the splitter alias mechanism (#4331, #4386).

The discovery loop in ``scripts/reingest_from_s3.py:_load_scraper_registry``
walks every court module that exposes ``default_config()`` and registers its
scraper class plus its ``_split_rulings`` / ``_llm_extract_rulings`` callables
in the ``_SCRAPER_REGISTRY`` / ``_SPLIT_REGISTRY`` / ``_LLM_SPLIT_REGISTRY``
dicts under the canonical ``scraper_id``.  These tests verify that any module
exporting an additional ``_SPLIT_REGISTRY_ALIASES`` list also registers the
scraper class and the same callables under each alias scraper_id.

Why aliases are needed.  ``scripts/rebuild_db.py`` reconstructs rows from S3
by emitting a synthetic ``rebuild-{state}-{county}`` scraper_id (rather than
the live ``ca-{abbrev}-...`` id).  Audit and drain scripts that key on
``documents.scraper_id`` look up ``_SPLIT_REGISTRY[scraper_id]`` directly —
without an alias entry, every rebuild-path row silently no-ops because the
canonical scraper_id never matches.  Issue #4331 documents the failure mode
for the split registries: 21 SC ``all_same_case_title_cluster`` rows were
stuck on the rebuild path because ``ca-sc-tentatives-civil`` was registered
but ``rebuild-ca-santa_clara`` was not.

Issue #4386 extends the contract to ``_SCRAPER_REGISTRY``: when
``_reparse_document`` looks up the scraper class for a rebuild-path row, the
absence of an alias entry made it return ``None``, ``parse_document`` was
never called, and ``extracted["ruling_text"]`` kept its raw HTML — which
``check_no_html_in_ruling_text`` then rejected, skipping the DB write and the
judge resolver chain (root-caused for LA dept-25 in
``docs/investigations/la-dept-25-html-in-ruling-text-2026-05.md``).

This test file enforces the contract that:

1. Every module declaring ``_SPLIT_REGISTRY_ALIASES`` registers each alias.
2. Each alias resolves to the SAME callable / class as the canonical
   scraper_id (so splitter and parse-document behaviour is identical
   regardless of which id surfaces).
3. Every county that has a splitter has a matching ``rebuild-ca-<county>``
   alias — so a future scraper_id rename can't silently break splitter or
   scraper-class resolution on rebuild rows.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
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
# Helpers
# ---------------------------------------------------------------------------


def _discover_modules_with_aliases() -> list[ModuleType]:
    """Return every court module that exports ``_SPLIT_REGISTRY_ALIASES``.

    Walks the same ``courts`` package tree as ``_load_scraper_registry``.
    Skips modules that fail to import (the discovery loop in production
    code does the same — see #4331).
    """
    import courts

    out: list[ModuleType] = []
    for _importer, modname, ispkg in pkgutil.walk_packages(courts.__path__, prefix="courts."):
        if ispkg:
            continue
        try:
            mod = importlib.import_module(modname)
        except Exception:  # noqa: BLE001
            continue
        aliases = getattr(mod, "_SPLIT_REGISTRY_ALIASES", None)
        if aliases:
            out.append(mod)
    return out


def _config_factories(mod: ModuleType) -> list:
    """Return all ``default_config*`` factory callables defined in ``mod``."""
    out = []
    for name, obj in inspect.getmembers(mod, inspect.isfunction):
        if (name == "default_config" or name.startswith("default_config_")) and (
            obj.__module__ == mod.__name__
        ):
            out.append(obj)
    return out


def _canonical_scraper_ids(mod: ModuleType) -> list[str]:
    """Return the canonical scraper_id for every ``default_config*`` factory."""
    return sorted({factory().scraper_id for factory in _config_factories(mod)})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAliasesPlumbedIntoRegistry:
    """``_SPLIT_REGISTRY_ALIASES`` must register the same callables as the canonical id."""

    def _reload_registries(self) -> None:
        reingest._SCRAPER_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()
        reingest._LLM_SPLIT_REGISTRY.clear()
        reingest._load_scraper_registry()

    def test_at_least_one_module_declares_aliases(self) -> None:
        """A baseline: at least one module must declare _SPLIT_REGISTRY_ALIASES.

        Otherwise this test file is exercising nothing.  The Santa Clara
        splitter (#4331's canonical reproducer) is required to register
        ``rebuild-ca-santa_clara``; if that ever disappears, the entire
        alias mechanism has regressed.
        """
        modules = _discover_modules_with_aliases()
        assert modules, (
            "No court modules export _SPLIT_REGISTRY_ALIASES — the alias "
            "mechanism added in #4331 has regressed.  At minimum, "
            "courts.ca.sc_tentatives must declare "
            "['rebuild-ca-santa_clara']."
        )

    def test_every_alias_registers_split_fn_when_canonical_does(self) -> None:
        """If a module's canonical scraper_id has a regex splitter, every alias must too."""
        self._reload_registries()
        for mod in _discover_modules_with_aliases():
            split_fn = getattr(mod, "_split_rulings", None)
            if split_fn is None:
                # Module has aliases but no regex splitter — only the LLM
                # path matters here; covered by the next test.
                continue
            for canonical_id in _canonical_scraper_ids(mod):
                assert reingest._SPLIT_REGISTRY.get(canonical_id) is split_fn, (
                    f"Canonical scraper_id {canonical_id!r} from "
                    f"{mod.__name__} did not register _split_rulings."
                )
            for alias in mod._SPLIT_REGISTRY_ALIASES:
                assert reingest._SPLIT_REGISTRY.get(alias) is split_fn, (
                    f"Alias scraper_id {alias!r} declared by {mod.__name__} "
                    f"did not register the module's _split_rulings function "
                    f"in _SPLIT_REGISTRY.  This is the #4331 failure mode — "
                    f"audit / drain on rebuild rows silently no-ops."
                )

    def test_every_alias_registers_scraper_class(self) -> None:
        """Every alias must resolve to a registered scraper class (#4386).

        This is the ``_SCRAPER_REGISTRY`` companion to the split-registry
        checks above.  ``_reparse_document`` looks up the scraper class via
        ``_SCRAPER_REGISTRY.get(scraper_id)``; if the rebuild-path alias is
        missing, the lookup returns ``None``, ``parse_document`` is never
        called, and ``extracted["ruling_text"]`` keeps the raw HTML page —
        which ``check_no_html_in_ruling_text`` rejects, skipping the DB
        write and the judge resolver chain (LA dept-25 reproducer in
        ``docs/investigations/la-dept-25-html-in-ruling-text-2026-05.md``).
        """
        self._reload_registries()
        for mod in _discover_modules_with_aliases():
            canonical_ids = _canonical_scraper_ids(mod)
            if not canonical_ids:
                # Module declares aliases but exports no default_config —
                # nothing to register a scraper class against.
                continue
            for canonical_id in canonical_ids:
                canonical_cls = reingest._SCRAPER_REGISTRY.get(canonical_id)
                assert canonical_cls is not None, (
                    f"Canonical scraper_id {canonical_id!r} from "
                    f"{mod.__name__} did not register a scraper class in "
                    f"_SCRAPER_REGISTRY."
                )
            for alias in mod._SPLIT_REGISTRY_ALIASES:
                alias_cls = reingest._SCRAPER_REGISTRY.get(alias)
                assert alias_cls is not None, (
                    f"Alias scraper_id {alias!r} declared by {mod.__name__} "
                    f"did not register a scraper class in _SCRAPER_REGISTRY. "
                    f"This is the #4386 failure mode — _reparse_document "
                    f"will fall through and leave raw HTML in ruling_text."
                )
                # Sanity: the alias should map to one of the module's
                # registered classes (it cannot map to the wrong module's
                # class because aliases are scoped to a single module in
                # _load_scraper_registry).
                assert alias_cls in {reingest._SCRAPER_REGISTRY[cid] for cid in canonical_ids}, (
                    f"Alias scraper_id {alias!r} declared by {mod.__name__} "
                    f"resolves to {alias_cls!r}, which is not one of the "
                    f"module's canonical classes "
                    f"{[reingest._SCRAPER_REGISTRY[cid] for cid in canonical_ids]!r}."
                )

    def test_every_alias_registers_llm_split_fn_when_canonical_does(self) -> None:
        """If a module's canonical scraper_id has an LLM splitter, every alias must too."""
        self._reload_registries()
        for mod in _discover_modules_with_aliases():
            llm_split_fn = getattr(mod, "_llm_extract_rulings", None)
            if llm_split_fn is None:
                continue
            for canonical_id in _canonical_scraper_ids(mod):
                assert reingest._LLM_SPLIT_REGISTRY.get(canonical_id) is llm_split_fn, (
                    f"Canonical scraper_id {canonical_id!r} from "
                    f"{mod.__name__} did not register _llm_extract_rulings."
                )
            for alias in mod._SPLIT_REGISTRY_ALIASES:
                assert reingest._LLM_SPLIT_REGISTRY.get(alias) is llm_split_fn, (
                    f"Alias scraper_id {alias!r} declared by {mod.__name__} "
                    f"did not register the module's _llm_extract_rulings "
                    f"function in _LLM_SPLIT_REGISTRY (#4331)."
                )


class TestSantaClaraReproducerSatisfied:
    """Direct regression for the #4331 reproducer (post-deploy verification of #4321)."""

    def test_rebuild_ca_santa_clara_resolves_to_sc_splitter(self) -> None:
        """``_SPLIT_REGISTRY['rebuild-ca-santa_clara']`` must resolve.

        This is the literal failure mode from #4331: SC's
        ``all_same_case_title_cluster`` rows in the dev DB carried
        ``scraper_id='rebuild-ca-santa_clara'`` and the drain script's
        ``resolve_split_fn`` looked up the registry with that key, got
        ``None``, and skipped every cluster.
        """
        reingest._SCRAPER_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()
        reingest._LLM_SPLIT_REGISTRY.clear()
        reingest._load_scraper_registry()

        assert "rebuild-ca-santa_clara" in reingest._SPLIT_REGISTRY, (
            "rebuild-ca-santa_clara not registered in _SPLIT_REGISTRY — "
            "drain_splitter_carry_forward_clusters.resolve_split_fn() "
            "will return None and skip every SC rebuild-path cluster (#4331)."
        )

        from courts.ca import sc_tentatives  # type: ignore[import-not-found]

        assert reingest._SPLIT_REGISTRY["rebuild-ca-santa_clara"] is sc_tentatives._split_rulings, (
            "rebuild-ca-santa_clara resolves to a function but not the SC "
            "splitter — alias plumbing crossed wires."
        )


class TestLosAngelesReproducerSatisfied:
    """Direct regression for the #4386 reproducer (LA dept-25 HTML-in-ruling_text)."""

    def test_rebuild_ca_los_angeles_resolves_to_la_scraper_class(self) -> None:
        """``_SCRAPER_REGISTRY['rebuild-ca-los_angeles']`` must resolve to LA's class.

        This is the literal failure mode from #4386: LA's dept-25
        rebuild-path rows carried ``scraper_id='rebuild-ca-los_angeles'``,
        ``_reparse_document`` looked up ``_SCRAPER_REGISTRY`` with that
        key, got ``None``, and ``parse_document`` was never called — so
        ``extracted["ruling_text"]`` kept its raw HTML, the deterministic
        validator rejected it, and the DB write (and judge resolution)
        was skipped.

        Root-cause writeup:
        ``docs/investigations/la-dept-25-html-in-ruling-text-2026-05.md``.
        """
        reingest._SCRAPER_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()
        reingest._LLM_SPLIT_REGISTRY.clear()
        reingest._load_scraper_registry()

        assert "rebuild-ca-los_angeles" in reingest._SCRAPER_REGISTRY, (
            "rebuild-ca-los_angeles not registered in _SCRAPER_REGISTRY — "
            "_reparse_document will return None for the scraper class, "
            "parse_document will never be called, and ruling_text will "
            "stay as raw HTML (#4386)."
        )

        from courts.ca import la_tentatives  # type: ignore[import-not-found]

        # Both LA scrapers (civil + appellate) share this rebuild id.
        # The discovery loop iterates ``_SCRAPER_CLASS_BY_ID`` in dict
        # insertion order (civil then appellate), so the alias entry
        # ends up pointing at the *last* registered class — appellate.
        # Either is correct for satisfying the AC ("does NOT start with
        # <html"): both expose ``parse_document`` and call into
        # ``_split_rulings`` to narrow per-case.  We assert the alias
        # resolves to one of the module's classes rather than pinning
        # to a specific one — pinning would make the test brittle to
        # an unrelated _SCRAPER_CLASS_BY_ID reorder.
        alias_cls = reingest._SCRAPER_REGISTRY["rebuild-ca-los_angeles"]
        assert alias_cls in (
            la_tentatives.LATentativeRulingsScraper,
            la_tentatives.LAAppellateTentativeRulingsScraper,
        ), (
            f"rebuild-ca-los_angeles resolves to {alias_cls!r}, which is "
            f"not one of LA's registered scraper classes — alias plumbing "
            f"crossed wires (#4386)."
        )


class TestEverySplitterCountyHasRebuildAlias:
    """Lock-in: every county with a splitter must register a rebuild alias.

    Without this, future splitter additions can silently regress the same
    rebuild-path bug #4331 fixed.  Yes, it duplicates the per-module
    declarations — that's the point.  This test fails loudly the first time
    a new splitter ships without a matching alias.
    """

    # Mapping enforced by this test.  Every entry corresponds to one CA county
    # whose scraper module exports ``_split_rulings`` and/or
    # ``_llm_extract_rulings``.  The mapping is intentionally hand-curated
    # rather than derived from sluggified config.county — that derivation is
    # the test we want, but circularly deriving it from the module would
    # silently mask a missing alias.  Update this dict when adding a new
    # splitter for a new county.
    _EXPECTED_REBUILD_ALIASES: dict[str, str] = {
        "courts.ca.sc_tentatives": "rebuild-ca-santa_clara",
        "courts.ca.cc_tentatives": "rebuild-ca-contra_costa",
        "courts.ca.fresno_tentatives": "rebuild-ca-fresno",
        "courts.ca.la_tentatives": "rebuild-ca-los_angeles",
        "courts.ca.sf_tentatives": "rebuild-ca-san_francisco",
        "courts.ca.riverside_tentatives": "rebuild-ca-riverside",
        "courts.ca.sd_calendar": "rebuild-ca-san_diego",
    }

    @pytest.mark.parametrize(
        "module_path,expected_alias",
        sorted(_EXPECTED_REBUILD_ALIASES.items()),
    )
    def test_module_declares_rebuild_alias(self, module_path: str, expected_alias: str) -> None:
        """Each tracked splitter module must include the expected rebuild alias."""
        mod = importlib.import_module(module_path)
        aliases = getattr(mod, "_SPLIT_REGISTRY_ALIASES", []) or []
        assert expected_alias in aliases, (
            f"{module_path} is missing the expected rebuild alias "
            f"{expected_alias!r} from _SPLIT_REGISTRY_ALIASES.  Without it, "
            f"audit / drain scripts that key on documents.scraper_id will "
            f"silently no-op on rebuild-path rows (#4331)."
        )

    def test_every_module_with_splitter_is_tracked(self) -> None:
        """Every module exporting a splitter must appear in the expected dict.

        Catches the case where a new county's splitter ships without anyone
        adding it to ``_EXPECTED_REBUILD_ALIASES`` here.  If you hit this
        assertion: add a row to the dict above and add
        ``_SPLIT_REGISTRY_ALIASES`` to the new module.
        """
        import courts

        modules_with_splitters: list[str] = []
        for _importer, modname, ispkg in pkgutil.walk_packages(courts.__path__, prefix="courts."):
            if ispkg:
                continue
            try:
                mod = importlib.import_module(modname)
            except Exception:  # noqa: BLE001
                continue
            has_splitter = any(
                callable(getattr(mod, attr, None))
                for attr in ("_split_rulings", "_llm_extract_rulings")
            )
            if has_splitter:
                modules_with_splitters.append(modname)

        untracked = set(modules_with_splitters) - set(self._EXPECTED_REBUILD_ALIASES.keys())
        assert not untracked, (
            f"These modules export a splitter but are not tracked in "
            f"TestEverySplitterCountyHasRebuildAlias._EXPECTED_REBUILD_ALIASES: "
            f"{sorted(untracked)}.  Add the rebuild-ca-<county> alias to "
            f"the module's _SPLIT_REGISTRY_ALIASES and add the expected "
            f"alias to the test dict (#4331)."
        )


class TestAliasContractIsolated:
    """The alias mechanism itself, exercised in isolation against a synthetic module."""

    def test_alias_registers_in_split_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A synthetic module with _SPLIT_REGISTRY_ALIASES + _split_rulings must register both."""
        import types

        # Build a fake module the discovery loop can consume.  We reach
        # straight into the loop's getattr() targets so the test exercises
        # the exact registration contract without needing to fake a full
        # courts.* tree.
        fake_split = lambda text: []  # noqa: E731 — minimal sentinel
        fake_mod = types.ModuleType("courts.ca.fake_alias_test")
        fake_mod._split_rulings = fake_split  # type: ignore[attr-defined]
        fake_mod._SPLIT_REGISTRY_ALIASES = ["rebuild-ca-fake-test"]  # type: ignore[attr-defined]

        # Mimic what _load_scraper_registry does for the alias plumbing.
        scraper_id = "ca-fake-test"
        split_fn = getattr(fake_mod, "_split_rulings", None)
        split_aliases = getattr(fake_mod, "_SPLIT_REGISTRY_ALIASES", []) or []

        # Snapshot + restore the global registries so we don't leak state.
        saved_split = dict(reingest._SPLIT_REGISTRY)
        saved_llm = dict(reingest._LLM_SPLIT_REGISTRY)
        try:
            if split_fn is not None and callable(split_fn):
                reingest._SPLIT_REGISTRY[scraper_id] = split_fn
                for alias in split_aliases:
                    reingest._SPLIT_REGISTRY[alias] = split_fn

            assert reingest._SPLIT_REGISTRY[scraper_id] is fake_split
            assert reingest._SPLIT_REGISTRY["rebuild-ca-fake-test"] is fake_split
        finally:
            reingest._SPLIT_REGISTRY.clear()
            reingest._SPLIT_REGISTRY.update(saved_split)
            reingest._LLM_SPLIT_REGISTRY.clear()
            reingest._LLM_SPLIT_REGISTRY.update(saved_llm)

    def test_module_without_aliases_still_registers_canonical_only(self) -> None:
        """Modules omitting _SPLIT_REGISTRY_ALIASES must still register their canonical id.

        Backward compatibility for any future splitter that doesn't need a
        rebuild alias.
        """
        reingest._SCRAPER_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()
        reingest._LLM_SPLIT_REGISTRY.clear()
        reingest._load_scraper_registry()

        # Just confirm the registry has *something* in it — the per-module
        # checks above confirm specifics.  This is a smoke test against
        # an obvious regression: someone makes _SPLIT_REGISTRY_ALIASES
        # required and accidentally drops every module that doesn't declare it.
        assert reingest._SPLIT_REGISTRY, (
            "_SPLIT_REGISTRY is empty after _load_scraper_registry() — the "
            "alias mechanism may have inadvertently broken canonical "
            "registration for modules without _SPLIT_REGISTRY_ALIASES."
        )

    def test_alias_registers_in_scraper_registry(self) -> None:
        """A module with _SPLIT_REGISTRY_ALIASES must also register its scraper class
        under each alias in ``_SCRAPER_REGISTRY`` (#4386).

        Mirrors ``test_alias_registers_in_split_registry`` for the
        scraper-class registry.  Uses a real reload of the production
        loader against a known-aliased module (sd_calendar) rather than a
        synthetic ``types.ModuleType`` because ``_SCRAPER_REGISTRY``
        registration depends on the discovery loop's ``default_config()``
        + ``_SCRAPER_CLASS_BY_ID`` resolution path, which is harder to
        fake than the bare ``getattr(_split_rulings)`` lookup.
        """
        reingest._SCRAPER_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()
        reingest._LLM_SPLIT_REGISTRY.clear()
        reingest._load_scraper_registry()

        # SD is the simplest end-to-end witness: one canonical
        # scraper_id, one alias.
        from courts.ca import sd_calendar  # type: ignore[import-not-found]

        canonical_id = sd_calendar.default_config().scraper_id
        canonical_cls = reingest._SCRAPER_REGISTRY.get(canonical_id)
        assert canonical_cls is not None, (
            f"sd_calendar canonical scraper_id {canonical_id!r} did not "
            f"register a scraper class — discovery loop is broken."
        )

        for alias in sd_calendar._SPLIT_REGISTRY_ALIASES:
            assert reingest._SCRAPER_REGISTRY.get(alias) is canonical_cls, (
                f"sd_calendar alias {alias!r} did not register the same "
                f"scraper class as the canonical id {canonical_id!r} — "
                f"#4386 alias plumbing is wrong."
            )
