"""Regression tests for #5165: ``ScraperConfig`` rejects unknown keys.

``ScraperConfig`` used pydantic's default ``extra="ignore"``, so a misspelled
or renamed keyword (``max_attempt=2``, ``request_delay=3.0``) was dropped and
the scraper silently ran with the default.  ``extra="forbid"`` makes every
unknown key a ``ValidationError``, and ``model_copy(update=...)`` (which
pydantic does not validate) refuses unknown keys too.

Because a config that raises would stop that scraper from capturing, this
module also builds every scraper config the repo ships: each runner registry
factory, and every ``default_config*`` factory under ``courts``.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

import courts
from framework.models import ScraperConfig
from framework.runner import _build_registry


def _config_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scraper_id": "test-5165",
        "state": "CA",
        "county": "Test",
        "court": "Superior Court",
        "target_urls": ["https://example.com"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Unknown keys are rejected
# ---------------------------------------------------------------------------


class TestScraperConfigForbidsExtra:
    def test_scraper_config_forbids_extra_kwarg(self) -> None:
        with pytest.raises(ValidationError, match="not_a_field"):
            ScraperConfig(**_config_kwargs(not_a_field=1))

    @pytest.mark.parametrize(
        "typo",
        ["max_attempt", "request_delay", "max_retries", "s3bucket", "poll_interval"],
    )
    def test_scraper_config_forbids_extra_typos(self, typo: str) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ScraperConfig(**_config_kwargs(**{typo: 1}))

    def test_scraper_config_forbids_extra_in_model_validate(self) -> None:
        with pytest.raises(ValidationError, match="not_a_field"):
            ScraperConfig.model_validate(_config_kwargs(not_a_field=1))

    def test_scraper_config_forbids_extra_in_model_copy(self) -> None:
        config = ScraperConfig(**_config_kwargs())
        with pytest.raises(ValidationError, match="request_delay"):
            config.model_copy(update={"request_delay": 0.0})

    def test_model_copy_validates_value_types(self) -> None:
        config = ScraperConfig(**_config_kwargs())
        with pytest.raises(ValidationError, match="max_attempts"):
            config.model_copy(update={"max_attempts": "not-a-number"})

    def test_model_copy_keeps_schedule_windows(self) -> None:
        from datetime import time

        config = ScraperConfig(
            **_config_kwargs(schedule_windows=[{"start": time(6), "end": time(9)}])
        )
        copied = config.model_copy(update={"max_attempts": 1})
        assert copied.schedule_windows == config.schedule_windows
        assert copied.schedule_windows is not config.schedule_windows
        assert copied.schedule_windows[0] is not config.schedule_windows[0]

    def test_model_copy_with_update_does_not_share_lists(self) -> None:
        config = ScraperConfig(**_config_kwargs())
        copied = config.model_copy(update={"max_attempts": 1})
        assert copied.target_urls is not config.target_urls
        config.target_urls.append("https://other.example.com")
        assert copied.target_urls == ["https://example.com"]

    def test_model_copy_with_known_keys_still_works(self) -> None:
        config = ScraperConfig(**_config_kwargs())
        copied = config.model_copy(update={"request_delay_seconds": 0.0, "max_attempts": 1})
        assert copied.request_delay_seconds == 0.0
        assert copied.max_attempts == 1
        assert config.request_delay_seconds == 1.0

    def test_model_copy_without_update_still_works(self) -> None:
        config = ScraperConfig(**_config_kwargs())
        assert config.model_copy() == config
        assert config.model_copy(deep=True) == config

    def test_known_fields_still_accepted(self) -> None:
        config = ScraperConfig(
            **_config_kwargs(
                poll_interval_seconds=60,
                request_delay_seconds=2.0,
                request_timeout_seconds=10.0,
                max_attempts=2,
                s3_bucket="bucket",
                s3_prefix="prefix",
            )
        )
        assert config.max_attempts == 2
        assert config.s3_bucket == "bucket"


# ---------------------------------------------------------------------------
# Every scraper config the repo ships still constructs
# ---------------------------------------------------------------------------


def _registry_ids() -> list[str]:
    return [scraper_id for scraper_id, _cls, _factory in _build_registry()]


@pytest.mark.parametrize("scraper_id", _registry_ids())
def test_every_registry_config_constructs(scraper_id: str) -> None:
    factory = {sid: f for sid, _cls, f in _build_registry()}[scraper_id]
    # runner._run_one_scraper calls each factory exactly like this.
    config = factory(s3_bucket="test-bucket")
    assert isinstance(config, ScraperConfig)
    assert config.s3_bucket == "test-bucket"


def _all_default_config_factories() -> list[tuple[str, Callable[..., ScraperConfig]]]:
    found: list[tuple[str, Callable[..., ScraperConfig]]] = []
    for mod_info in pkgutil.walk_packages(courts.__path__, prefix="courts."):
        module = importlib.import_module(mod_info.name)
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("default_config") and obj.__module__ == module.__name__:
                found.append((f"{module.__name__}.{name}", obj))
    return sorted(found, key=lambda item: item[0])


_FACTORIES = _all_default_config_factories()


def test_factory_discovery_covers_registry() -> None:
    discovered = {factory for _name, factory in _FACTORIES}
    for scraper_id, _cls, factory in _build_registry():
        assert factory in discovered, scraper_id


@pytest.mark.parametrize("factory", [f for _n, f in _FACTORIES], ids=[n for n, _f in _FACTORIES])
def test_every_courts_default_config_constructs(factory: Callable[..., ScraperConfig]) -> None:
    assert isinstance(factory(), ScraperConfig)
    assert isinstance(factory(s3_bucket="test-bucket"), ScraperConfig)
