"""Static guard tests for scrapers that use LLM-based ruling extraction.

Scraper modules that expose a module-level ``_llm_extract_rulings`` function
split multi-ruling pages with an LLM at capture time.
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
in a module exporting ``_llm_extract_rulings`` must have a ``parse_document`` that
references ``pre_split`` or ``_llm_extracted``. The check auto-adapts as
scrapers are added or removed.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest


def _llm_split_scrapers() -> dict[str, type]:
    """Return ``{"<module>.<Class>": scraper class}`` for every scraper class
    defined in a ``courts`` module that exports ``_llm_extract_rulings``."""
    import courts
    from framework.base import BaseScraper

    found: dict[str, type] = {}
    for _importer, modname, ispkg in pkgutil.walk_packages(courts.__path__, prefix="courts."):
        if ispkg:
            continue
        mod = importlib.import_module(modname)
        if not callable(getattr(mod, "_llm_extract_rulings", None)):
            continue
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, BaseScraper)
                and obj is not BaseScraper
                and obj.__module__ == modname
            ):
                found[f"{modname}.{name}"] = obj
    return found


# Parametrize at collection time so each scraper gets its own test ID.
_LLM_SCRAPERS = _llm_split_scrapers()


class TestLlmSplitGuards:
    """Every scraper with ``_llm_extract_rulings`` must guard ``parse_document``."""

    def test_discovery_is_non_empty(self) -> None:
        """Sanity check: at least one scraper should expose ``_llm_extract_rulings``.

        If this fails, either (a) no scrapers use LLM extraction anymore
        (remove this test suite), or (b) discovery is broken.
        """
        if not _LLM_SCRAPERS:
            pytest.fail(
                "No scraper modules export _llm_extract_rulings — "
                "guard test cannot validate any scrapers; discovery may be broken"
            )

    @pytest.mark.parametrize("name", sorted(_LLM_SCRAPERS), ids=sorted(_LLM_SCRAPERS))
    def test_parse_document_guards_pre_split(self, name: str) -> None:
        """``parse_document`` source must mention ``pre_split`` or ``_llm_extracted``.

        The guard is checked by substring match on the method source because
        the bug we are preventing is structural: forgetting to short-circuit
        on pre-split docs. A simple substring check catches that reliably
        without needing a full AST walk — any guard implementation
        (early return, if/else, conditional assignment) will reference one
        of the two flag names.
        """
        scraper_cls = _LLM_SCRAPERS[name]
        try:
            source = inspect.getsource(scraper_cls.parse_document)
        except (OSError, TypeError) as exc:
            pytest.fail(f"Could not read parse_document source for {name}: {exc}")

        has_guard = "pre_split" in source or "_llm_extracted" in source
        assert has_guard, (
            f"{name} exports _llm_extract_rulings but its parse_document method "
            f"does not reference 'pre_split' or '_llm_extracted'. Without this "
            f"guard, parse_document will re-run regex over the full multi-case PDF "
            f"text and clobber the LLM-populated per-ruling fields (case_number, "
            f"case_title, ruling_text, motion_type, outcome, parties). See #2469 "
            f"and #2484 for background. Fix: add an early-return check at the top "
            f"of parse_document, e.g.:\n"
            f"    if doc.extra.get('pre_split') or doc.extra.get('_llm_extracted'):\n"
            f"        return doc"
        )
