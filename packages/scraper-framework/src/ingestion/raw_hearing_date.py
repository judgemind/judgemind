"""Hearing dates for ingestion events built straight from S3 (#4774).

Live scrapers put an authoritative ``hearing_date`` on every event, taken
from the PDF filename, a labelled header, or a listing page.  ``rebuild_db``
and prefix-mode ``reingest_from_s3`` build events from the raw S3 object
instead, so ``parse_document`` never runs and the event has no date.  Split
children then guessed: NULL, or a date from the ruling body (#4667 Santa
Clara, #4769 Contra Costa).

Each scraper now owns its derivation through the
:meth:`framework.base.BaseScraper.hearing_date_for_raw` classmethod.  This
module picks the scraper for an event and calls its hook, so the worker
needs no per-county branch.

Picking the scraper:

1. ``capture_scraper_id`` (the live scraper id the archiver stamped on the
   S3 object, restored by the rebuild / prefix event builders), else the
   event's own ``scraper_id`` when it names a live scraper.  That class's
   hook is authoritative, even when it returns None.
2. Otherwise (e.g. a rebuild from a local S3 cache, which has no object
   metadata) every scraper registered for the event's state and county is
   asked.  A date is used only when all the hooks that return one agree;
   disagreement returns None rather than a guess.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import pkgutil
from collections import defaultdict
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _overrides_hook(cls: type) -> bool:
    from framework.base import BaseScraper

    return cls.hearing_date_for_raw.__func__ is not BaseScraper.hearing_date_for_raw.__func__  # type: ignore[attr-defined]


@functools.cache
def _scraper_index() -> tuple[dict[str, type], dict[tuple[str, str], tuple[type, ...]]]:
    """Map live scraper ids, and (STATE, COUNTY) pairs, to scraper classes.

    Discovered from every ``courts`` module that exposes a ``default_config``
    factory, the same convention ``reingest_from_s3`` uses, so a new scraper
    is covered without registering it here.  Multi-scraper modules name
    their classes through ``_SCRAPER_CLASS_BY_ID``.
    """
    from framework.base import BaseScraper

    by_id: dict[str, type] = {}
    by_county: dict[tuple[str, str], list[type]] = defaultdict(list)
    try:
        import courts
    except ImportError:  # pragma: no cover - courts ships with this package
        return by_id, {}

    for modinfo in pkgutil.walk_packages(courts.__path__, prefix="courts."):
        if modinfo.ispkg:
            continue
        try:
            mod = importlib.import_module(modinfo.name)
        except Exception:
            # Once per process: this scraper's hook is unavailable, so its
            # rebuild/reingest events fall back to LLM/regex dates.
            logger.warning("raw_hearing_date: cannot import %s", modinfo.name, exc_info=True)
            continue
        class_by_id: dict[str, type] = getattr(mod, "_SCRAPER_CLASS_BY_ID", None) or {}
        classes = [
            obj
            for _, obj in inspect.getmembers(mod, inspect.isclass)
            if issubclass(obj, BaseScraper)
            and obj is not BaseScraper
            and obj.__module__ == mod.__name__
        ]
        for name, factory in inspect.getmembers(mod, inspect.isfunction):
            if factory.__module__ != mod.__name__:
                continue
            if name != "default_config" and not name.startswith("default_config_"):
                continue
            try:
                config = factory()
            except Exception:
                logger.debug("raw_hearing_date: %s.%s() failed", modinfo.name, name)
                continue
            cls = class_by_id.get(config.scraper_id) or (classes[0] if len(classes) == 1 else None)
            if cls is None:
                continue
            by_id[config.scraper_id] = cls
            key = (config.state.upper(), config.county.upper())
            if cls not in by_county[key]:
                by_county[key].append(cls)
    return by_id, {k: tuple(v) for k, v in by_county.items()}


def _call_hook(
    cls: type,
    text: str,
    source_url: str,
    content_format: str,
    capture_timestamp: datetime | None,
) -> datetime | None:
    try:
        return cls.hearing_date_for_raw(  # type: ignore[attr-defined]
            text,
            source_url=source_url,
            content_format=content_format,
            capture_timestamp=capture_timestamp,
        )
    except Exception:
        logger.warning(
            "hearing_date_for_raw raised; treating as no date",
            extra={"scraper_class": cls.__name__},
            exc_info=True,
        )
        return None


def _parse_capture_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def raw_hearing_date(event_data: dict[str, Any], text: str | None) -> str | None:
    """Return the live-scraper hearing date for *event_data* as ``YYYY-MM-DD``.

    *text* is the document text the worker will split: extracted PDF text,
    or the raw markup for HTML.  Returns None when no scraper derives a date
    from this raw (see the module docstring for how the scraper is picked).
    """
    if not text:
        return None
    by_id, by_county = _scraper_index()
    source_url = str(event_data.get("source_url") or "")
    content_format = str(event_data.get("content_format") or "")
    capture_timestamp = _parse_capture_timestamp(event_data.get("capture_timestamp"))

    cls = by_id.get(str(event_data.get("capture_scraper_id") or "")) or by_id.get(
        str(event_data.get("scraper_id") or "")
    )
    if cls is not None:
        result = _as_date(_call_hook(cls, text, source_url, content_format, capture_timestamp))
        return result.isoformat() if result is not None else None

    key = (
        str(event_data.get("state") or "").upper(),
        str(event_data.get("county") or "").upper(),
    )
    dates: set[date] = set()
    for candidate in by_county.get(key, ()):
        if not _overrides_hook(candidate):
            continue
        found = _as_date(_call_hook(candidate, text, source_url, content_format, capture_timestamp))
        if found is not None:
            dates.add(found)
    if len(dates) > 1:
        logger.info(
            "raw_hearing_date: county scrapers disagree; leaving hearing_date unset",
            extra={
                "document_id": event_data.get("document_id"),
                "county": event_data.get("county"),
                "candidates": sorted(str(d) for d in dates),
            },
        )
        return None
    return dates.pop().isoformat() if dates else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None
