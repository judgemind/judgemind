"""Regression tests for #4732: every roster directory class must define ``COURT_ID``.

``framework.runner._fetch_rosters`` reads ``cls.COURT_ID`` on each directory
class. ``SantaClaraCourtDirectory`` once shipped without it, so the Santa Clara
roster prefetch failed with an ``AttributeError`` on every scraper run.

The directory classes are discovered by walking the ``courts`` package rather
than by hand-listing them, so a new directory class that forgets ``COURT_ID``
fails here whether or not it has been wired into ``_ROSTER_DIRECTORIES`` yet.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re

import courts
from framework import runner
from framework.court_directory import CourtDirectory

_COURT_ID_RE = re.compile(r"^[a-z]{2}_[a-z0-9_]+$")


def _discover_directory_classes() -> list[type[CourtDirectory]]:
    """Import every module under ``courts`` and return concrete CourtDirectory subclasses."""
    found: dict[str, type[CourtDirectory]] = {}
    for info in pkgutil.walk_packages(courts.__path__, prefix="courts."):
        module = importlib.import_module(info.name)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, CourtDirectory)
                and obj is not CourtDirectory
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__
            ):
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return [found[key] for key in sorted(found)]


def _assert_has_court_id(cls: type) -> None:
    court_id = cls.__dict__.get("COURT_ID")
    assert isinstance(court_id, str), (
        f"{cls.__module__}.{cls.__qualname__} must define a class attribute "
        f'`COURT_ID: str = "<state>_<county>"` (runner._fetch_rosters reads cls.COURT_ID)'
    )
    assert _COURT_ID_RE.match(court_id), (
        f"{cls.__module__}.{cls.__qualname__}.COURT_ID={court_id!r} is not a "
        f"lower-case '<state>_<court>' id"
    )


def test_discovery_finds_directory_classes() -> None:
    classes = _discover_directory_classes()
    names = {cls.__qualname__ for cls in classes}
    # Sanity floor so a broken walk cannot pass vacuously.
    assert "SantaClaraCourtDirectory" in names
    assert "LACourtDirectory" in names
    assert len(classes) >= 10


def test_all_discovered_roster_directories_have_court_id() -> None:
    for cls in _discover_directory_classes():
        _assert_has_court_id(cls)


def test_runner_roster_directories_have_court_id() -> None:
    for module_path, class_name in runner._ROSTER_DIRECTORIES:
        cls = getattr(importlib.import_module(module_path), class_name)
        assert issubclass(cls, CourtDirectory)
        _assert_has_court_id(cls)


def test_santa_clara_directory_court_id() -> None:
    from courts.ca.sc_tentatives import COURT_ID, SantaClaraCourtDirectory

    assert SantaClaraCourtDirectory.COURT_ID == "ca_santa_clara"
    assert SantaClaraCourtDirectory.COURT_ID == COURT_ID
