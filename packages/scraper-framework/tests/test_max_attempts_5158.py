"""Regression tests for #5158: attempt counters are named ``max_attempts``.

``ScraperConfig.max_retries`` and ``IngestionWorker(max_retries=...)`` both
counted total attempts (first try included), not retries after the first.
Both are renamed to ``max_attempts``; behaviour (the number of attempts)
is unchanged.  An old ``max_retries=`` kwarg must fail loudly rather than be
silently ignored and fall back to the default.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from framework.models import ScraperConfig
from ingestion.worker import DEFAULT_MAX_ATTEMPTS, IngestionWorker


def _config_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scraper_id": "test-5158",
        "state": "CA",
        "county": "Test",
        "court": "Superior Court",
        "target_urls": ["https://example.com"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ScraperConfig
# ---------------------------------------------------------------------------


class TestScraperConfigMaxAttempts:
    def test_default_is_three_attempts(self) -> None:
        assert ScraperConfig(**_config_kwargs()).max_attempts == 3

    def test_explicit_value_is_kept(self) -> None:
        assert ScraperConfig(**_config_kwargs(max_attempts=2)).max_attempts == 2

    def test_has_no_max_retries_field(self) -> None:
        assert "max_retries" not in ScraperConfig.model_fields
        assert "max_attempts" in ScraperConfig.model_fields

    def test_old_max_retries_kwarg_raises(self) -> None:
        # ScraperConfig forbids unknown keys (#5165), so the old name fails
        # loudly instead of running with the default attempt count.
        with pytest.raises(ValidationError, match="max_retries"):
            ScraperConfig(**_config_kwargs(max_retries=1))

    def test_old_key_raises_from_model_validate(self) -> None:
        with pytest.raises(ValidationError, match="max_retries"):
            ScraperConfig.model_validate(_config_kwargs(max_retries=1))


# ---------------------------------------------------------------------------
# IngestionWorker
# ---------------------------------------------------------------------------


def _worker(**kwargs: Any) -> IngestionWorker:
    return IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
        **kwargs,
    )


class TestIngestionWorkerMaxAttempts:
    def test_default_constant_is_three(self) -> None:
        assert DEFAULT_MAX_ATTEMPTS == 3

    def test_default_attempts(self) -> None:
        assert _worker()._max_attempts == 3

    def test_max_attempts_kwarg(self) -> None:
        assert _worker(max_attempts=5)._max_attempts == 5

    def test_old_max_retries_kwarg_raises(self) -> None:
        with pytest.raises(TypeError):
            _worker(max_retries=3)

    def test_dead_letters_after_max_attempts(self) -> None:
        worker = _worker(max_attempts=2)
        worker.process_event = MagicMock(side_effect=ValueError("boom"))  # type: ignore[method-assign]
        with patch("ingestion.worker.psycopg"):
            worker._process_event_data(b"msg-1", {"source_key": "k"})
        assert worker.process_event.call_count == 2
        worker._redis.xack.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Entrypoint env parsing
# ---------------------------------------------------------------------------


@pytest.fixture
def main_mod() -> Any:
    import ingestion.__main__ as mod

    return mod


class TestMaxAttemptsFromEnv:
    def test_neither_set_defaults_to_three(self, main_mod: Any) -> None:
        assert main_mod.max_attempts_from_env({}) == 3

    def test_max_attempts_only(self, main_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_logger = MagicMock()
        monkeypatch.setattr(main_mod, "logger", fake_logger)
        assert main_mod.max_attempts_from_env({"MAX_ATTEMPTS": "5"}) == 5
        fake_logger.warning.assert_not_called()

    def test_max_retries_env_is_not_read(self, main_mod: Any) -> None:
        # The MAX_RETRIES alias was dropped (#5165): no task definition sets it.
        assert main_mod.max_attempts_from_env({"MAX_RETRIES": "7"}) == 3
        assert main_mod.max_attempts_from_env({"MAX_ATTEMPTS": "2", "MAX_RETRIES": "7"}) == 2

    def test_empty_max_attempts_falls_through(self, main_mod: Any) -> None:
        assert main_mod.max_attempts_from_env({"MAX_ATTEMPTS": ""}) == 3

    def test_main_passes_max_attempts_to_worker(
        self, main_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {
            "DATABASE_URL": "postgresql://localhost/test",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "https://localhost:9200",
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "MAX_ATTEMPTS": "6",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("MAX_RETRIES", raising=False)
        with (
            patch.object(main_mod, "IngestionWorker") as worker_cls,
            patch.object(main_mod, "make_s3_client"),
            patch.object(main_mod, "make_opensearch_client"),
            patch.object(main_mod.redis, "Redis"),
        ):
            main_mod.main()
        assert worker_cls.call_args.kwargs["max_attempts"] == 6
        assert "max_retries" not in worker_cls.call_args.kwargs
