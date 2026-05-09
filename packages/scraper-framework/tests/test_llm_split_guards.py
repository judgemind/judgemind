"""Static guard tests for scrapers that use LLM-based ruling extraction.

Scraper modules that expose a module-level ``_llm_extract_rulings`` function
are picked up by ``scripts/reingest_from_s3.py``'s ``_LLM_SPLIT_REGISTRY``.
When LLM extraction is enabled, ``fetch_documents`` pre-populates per-ruling
fields (``case_number``, ``case_title``, ``ruling_text``, ``motion_type``,
``outcome``, ``parties``) on each split child document and marks it with
``doc.extra["pre_split"] = True`` or ``doc.extra["_llm_extracted"] = True``.

If the scraper's ``parse_document`` method does not guard against re-parsing
pre-split documents, it will re-run its regex extraction over the full PDF
text and clobber the correctly-populated LLM fields with whatever matches
first across the multi-case PDF. This bug has been hit and fixed in three
counties already (Fresno, LA, CC — see #2469 and PR #2478).

These tests statically enforce the invariant: every scraper class registered
in ``_LLM_SPLIT_REGISTRY`` must have a ``parse_document`` implementation that
references ``pre_split`` or ``_llm_extracted``. The check auto-adapts as
scrapers are added to or removed from the registry.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Import the reingest module from scripts/ (mirrors test_reingest_registry.py).
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


def _load_registries() -> tuple[dict, dict]:
    """Populate and return ``(_SCRAPER_REGISTRY, _LLM_SPLIT_REGISTRY)``.

    Clears and reloads the registries so that the test is not sensitive to
    prior state (other tests mutate these globals).
    """
    reingest._SCRAPER_REGISTRY.clear()
    reingest._LLM_SPLIT_REGISTRY.clear()
    reingest._SPLIT_REGISTRY.clear()
    reingest._load_scraper_registry()
    return reingest._SCRAPER_REGISTRY, reingest._LLM_SPLIT_REGISTRY


def _llm_scraper_ids() -> list[str]:
    """Return the list of scraper_ids in ``_LLM_SPLIT_REGISTRY`` that have a class.

    Post-#4386 (PR #4394 / #4401), aliases registered via a module's
    ``_SPLIT_REGISTRY_ALIASES`` (e.g. ``rebuild-ca-<county>``) ARE in
    ``_SCRAPER_REGISTRY`` — the registry-loader runs every registration
    through ``_register_with_aliases`` so canonical and alias keys land in
    all three registries together.  This filter is still load-bearing for
    a different reason: an alias may have an ``_LLM_SPLIT_REGISTRY`` entry
    that resolves to one of the module's classes via the alias entry,
    while the pre-split guard test below operates on ``parse_document``
    source — so we want every yielded scraper_id to be one we can pull a
    class for and ``inspect.getsource`` on without hitting None.

    See ``docs/investigations/rebuild-pattern-scraper-id-audit-2026-05.md``
    §4 for the post-#4386 docstring update rationale.
    """
    scraper_registry, llm = _load_registries()
    return sorted(sid for sid in llm if sid in scraper_registry)


# Parametrize at collection time so each scraper gets its own test ID.
_LLM_SCRAPER_IDS = _llm_scraper_ids()


class TestLlmSplitGuards:
    """Every scraper with ``_llm_extract_rulings`` must guard ``parse_document``."""

    def test_registry_is_non_empty(self) -> None:
        """Sanity check: at least one scraper should expose ``_llm_extract_rulings``.

        If this fails, either (a) no scrapers use LLM extraction anymore
        (remove this test suite), or (b) registry discovery is broken
        (debug ``_load_scraper_registry()``). The test suite protects an
        invariant that only matters when the registry is populated.
        """
        _, llm_registry = _load_registries()
        if not llm_registry:
            pytest.fail(
                "No scraper modules export _llm_extract_rulings — "
                "guard test cannot validate any scrapers; registry discovery may be broken"
            )

    @pytest.mark.parametrize("scraper_id", _LLM_SCRAPER_IDS, ids=_LLM_SCRAPER_IDS)
    def test_parse_document_guards_pre_split(self, scraper_id: str) -> None:
        """``parse_document`` source must mention ``pre_split`` or ``_llm_extracted``.

        The guard is checked by substring match on the method source because
        the bug we are preventing is structural: forgetting to short-circuit
        on pre-split docs. A simple substring check catches that reliably
        without needing a full AST walk — any guard implementation
        (early return, if/else, conditional assignment) will reference one
        of the two flag names.
        """
        scraper_registry, llm_registry = _load_registries()

        assert scraper_id in llm_registry, (
            f"Scraper {scraper_id!r} disappeared from _LLM_SPLIT_REGISTRY between "
            f"collection and execution — registry discovery is unstable"
        )
        assert scraper_id in scraper_registry, (
            f"Scraper {scraper_id!r} is in _LLM_SPLIT_REGISTRY but not in "
            f"_SCRAPER_REGISTRY — registry population is inconsistent"
        )

        scraper_cls = scraper_registry[scraper_id]
        module_name = scraper_cls.__module__

        try:
            source = inspect.getsource(scraper_cls.parse_document)
        except (OSError, TypeError) as exc:
            pytest.fail(
                f"Could not read parse_document source for {scraper_id!r} "
                f"({module_name}.{scraper_cls.__name__}): {exc}"
            )

        has_guard = "pre_split" in source or "_llm_extracted" in source
        assert has_guard, (
            f"Scraper {scraper_id!r} ({module_name}.{scraper_cls.__name__}) exports "
            f"_llm_extract_rulings but its parse_document method does not reference "
            f"'pre_split' or '_llm_extracted'. Without this guard, parse_document "
            f"will re-run regex over the full multi-case PDF text and clobber the "
            f"LLM-populated per-ruling fields (case_number, case_title, ruling_text, "
            f"motion_type, outcome, parties). See #2469 and #2484 for background. "
            f"Fix: add an early-return check at the top of parse_document, e.g.:\n"
            f"    if doc.extra.get('pre_split') or doc.extra.get('_llm_extracted'):\n"
            f"        return doc"
        )
