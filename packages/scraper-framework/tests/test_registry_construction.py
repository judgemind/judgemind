"""Smoke-test that every entry in _build_registry() can be instantiated.

This test would have caught the regression in #3499, where the runner passed
``db_conn`` to scraper constructors that did not accept it, causing a
TypeError that darkened 14 scrapers for days.

Each scraper is constructed with exactly the arguments the runner supplies:
  config=<default_config()>, archiver=None, event_bus=None, db_conn=None,
  **runner_extra_kwargs(scraper_id)

The ``runner_extra_kwargs`` helper mirrors the dept_judge_map injection in
``runner.py`` lines 577-585 — currently only the LA, Riverside, Ventura, and
SF-civil scrapers receive an extra ``dept_judge_map`` kwarg.
"""

from __future__ import annotations

import pytest

from framework.runner import _REGISTRY, _build_registry  # noqa: F401 (imported for side effects)

# ---------------------------------------------------------------------------
# Helper: mirror the extra_kwargs logic from runner.py
# ---------------------------------------------------------------------------

# These mirror runner.py exactly — only ca-la-tentatives-civil receives dept_judge_map,
# not the appellate variant.
_LA_SCRAPER_IDS = {"ca-la-tentatives-civil"}
_RIV_SCRAPER_IDS = {"ca-riverside-tentatives"}
_VENTURA_SCRAPER_IDS = {"ca-ventura-tentatives"}
_SF_CIVIL_SCRAPER_IDS = {"ca-sf-tentatives-civil"}


def _runner_extra_kwargs(scraper_id: str) -> dict[str, object]:
    """Return the extra kwargs runner.py would pass for this scraper_id.

    Mirrors the dept_judge_map injection block (runner.py lines 577-585).
    All dept_judge_map values are passed as an empty dict — the mapping is
    optional and the scraper must work without it.
    """
    extra: dict[str, object] = {}
    if scraper_id in _LA_SCRAPER_IDS:
        extra["dept_judge_map"] = {}
    if scraper_id in _RIV_SCRAPER_IDS:
        extra["dept_judge_map"] = {}
    if scraper_id in _VENTURA_SCRAPER_IDS:
        extra["dept_judge_map"] = {}
    if scraper_id in _SF_CIVIL_SCRAPER_IDS:
        extra["dept_judge_map"] = {}
    return extra


# ---------------------------------------------------------------------------
# Parametrized test
# ---------------------------------------------------------------------------


def _registry_params() -> list[tuple[str, type, object]]:
    """Collect all (scraper_id, scraper_cls, config_factory) triples."""
    _REGISTRY.clear()
    return list(_build_registry())


@pytest.mark.parametrize(
    "scraper_id,scraper_cls,config_factory",
    _registry_params(),
    ids=[entry[0] for entry in _registry_params()],
)
def test_registry_scraper_accepts_db_conn(
    scraper_id: str,
    scraper_cls: type,
    config_factory: object,
) -> None:
    """Every registry entry must instantiate without TypeError when db_conn is passed.

    This is the minimal smoke-test: if constructing a registered scraper with
    the runner's standard kwargs raises TypeError, the entire sweep will fail.
    """
    config = config_factory(s3_bucket="")  # type: ignore[operator]
    extra = _runner_extra_kwargs(scraper_id)

    # Must not raise TypeError (or any other exception)
    instance = scraper_cls(
        config=config,
        archiver=None,
        event_bus=None,
        db_conn=None,
        **extra,
    )
    assert instance is not None
