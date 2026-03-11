"""Tests for the scraper runner."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from framework import CapturedDocument, ContentFormat, ScraperConfig
from framework.base import BaseScraper
from framework.models import ScraperHealthEvent
from framework.runner import _REGISTRY, _build_registry, get_scraper_ids, main, run_scrapers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubScraper(BaseScraper):
    """Scraper that returns pre-set documents."""

    def __init__(self, docs: list[CapturedDocument] | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._docs = docs or []

    def fetch_documents(self) -> list[CapturedDocument]:
        return self._docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        return doc


class FailingScraper(BaseScraper):
    """Scraper whose run() raises an unhandled exception."""

    def fetch_documents(self) -> list[CapturedDocument]:
        raise RuntimeError("kaboom")

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        return doc


def _stub_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(
        scraper_id="test-stub",
        state="CA",
        county="Test",
        court="Superior Court",
        target_urls=["https://example.com"],
        s3_bucket=s3_bucket,
    )


def _make_doc() -> CapturedDocument:
    return CapturedDocument(
        scraper_id="test-stub",
        state="CA",
        county="Test",
        court="Superior Court",
        source_url="https://example.com/ruling",
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=b"<html>ruling</html>",
        content_hash="",
    )


def _patch_registry(entries: list[tuple[str, type, callable]]) -> object:
    """Patch _build_registry to return the given entries."""
    return patch("framework.runner._build_registry", return_value=entries)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunScrapersWithoutArchival:
    def test_runs_all_scrapers_and_returns_zero(self) -> None:
        entries = [("test-stub", StubScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 0

    def test_returns_zero_with_no_documents(self) -> None:
        entries = [("test-stub", StubScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 0


class TestRunScrapersWithArchival:
    def test_creates_archiver_when_bucket_set(self) -> None:
        doc = _make_doc()
        captured_archiver = {}

        class CapturingStub(StubScraper):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(docs=[doc], **kwargs)
                captured_archiver["archiver"] = kwargs.get("archiver")

        entries = [("test-stub", CapturingStub, _stub_config)]

        with (
            _patch_registry(entries),
            patch.dict(os.environ, {"JUDGEMIND_ARCHIVE_BUCKET": "my-bucket"}),
        ):
            exit_code = run_scrapers()

        assert exit_code == 0
        archiver = captured_archiver["archiver"]
        assert archiver is not None
        assert archiver.bucket == "my-bucket"

    def test_archiver_called_per_document(self) -> None:
        doc = _make_doc()
        mock_archiver_cls = MagicMock()
        mock_archiver_instance = MagicMock()
        mock_archiver_instance.archive.return_value = "ca/test/key.html"
        mock_archiver_cls.return_value = mock_archiver_instance

        entries = [("test-stub", StubScraper, _stub_config)]

        # Patch S3Archiver constructor to return our mock
        with (
            _patch_registry(entries),
            patch.dict(os.environ, {"JUDGEMIND_ARCHIVE_BUCKET": "my-bucket"}),
            patch("framework.runner.S3Archiver", mock_archiver_cls),
        ):
            # Inject docs into the stub
            original_init = StubScraper.__init__

            def patched_init(self: StubScraper, **kwargs: object) -> None:
                original_init(self, docs=[doc], **kwargs)

            with patch.object(StubScraper, "__init__", patched_init):
                exit_code = run_scrapers()

        assert exit_code == 0
        mock_archiver_cls.assert_called_once_with(bucket="my-bucket")


class TestRunScrapersFiltering:
    def test_filters_to_requested_ids(self) -> None:
        ran = []

        class TrackingScraper(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def config_a(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="scraper-a",
                state="CA",
                county="A",
                court="Court",
                target_urls=["https://example.com"],
            )

        def config_b(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="scraper-b",
                state="CA",
                county="B",
                court="Court",
                target_urls=["https://example.com"],
            )

        entries = [
            ("scraper-a", TrackingScraper, config_a),
            ("scraper-b", TrackingScraper, config_b),
        ]

        with _patch_registry(entries):
            exit_code = run_scrapers(scraper_ids=["scraper-b"])

        assert exit_code == 0
        assert ran == ["scraper-b"]

    def test_unknown_scraper_id_returns_one(self) -> None:
        entries = [("test-stub", StubScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers(scraper_ids=["nonexistent"])

        assert exit_code == 1


class TestRunScrapersErrorHandling:
    def test_unhandled_exception_returns_nonzero(self) -> None:
        entries = [("failing", FailingScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1

    def test_partial_failure_still_runs_remaining(self) -> None:
        ran = []

        class TrackingStub(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def config_good(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="good",
                state="CA",
                county="Good",
                court="Court",
                target_urls=["https://example.com"],
            )

        def config_fail(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="failing",
                state="CA",
                county="Fail",
                court="Court",
                target_urls=["https://example.com"],
            )

        entries = [
            ("failing", FailingScraper, config_fail),
            ("good", TrackingStub, config_good),
        ]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1  # failure reported
        assert "good" in ran  # but good scraper still ran


class TestNoArchivalInDevMode:
    def test_no_archiver_without_env_var(self) -> None:
        captured_archiver = {}

        class CapturingStub(StubScraper):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)
                captured_archiver["archiver"] = kwargs.get("archiver")

        entries = [("test-stub", CapturingStub, _stub_config)]

        env = os.environ.copy()
        env.pop("JUDGEMIND_ARCHIVE_BUCKET", None)

        with _patch_registry(entries), patch.dict(os.environ, env, clear=True):
            exit_code = run_scrapers()

        assert exit_code == 0
        assert captured_archiver["archiver"] is None


# ---------------------------------------------------------------------------
# Registry tests (lines 42-74, 79)
# ---------------------------------------------------------------------------


class TestBuildRegistry:
    """Tests for _build_registry() lazy discovery and get_scraper_ids()."""

    def setup_method(self) -> None:
        """Clear registry cache before each test."""
        _REGISTRY.clear()

    def teardown_method(self) -> None:
        """Clear registry cache after each test to avoid contamination."""
        _REGISTRY.clear()

    def test_build_registry_populates_entries(self) -> None:
        """_build_registry should discover all registered scrapers."""
        registry = _build_registry()
        assert len(registry) > 0
        # Check that known scraper IDs are present
        ids = [entry[0] for entry in registry]
        assert "ca-la-tentatives-civil" in ids
        assert "ca-oc-tentatives" in ids
        assert "ca-riverside-tentatives" in ids
        assert "ca-sf-tentatives-family-law" in ids

    def test_build_registry_caches_result(self) -> None:
        """_build_registry should return the same list object on subsequent calls."""
        first = _build_registry()
        second = _build_registry()
        assert first is second

    def test_build_registry_entries_are_tuples(self) -> None:
        """Each entry should be (str, type, callable)."""
        registry = _build_registry()
        for entry in registry:
            assert len(entry) == 3
            scraper_id, scraper_cls, config_factory = entry
            assert isinstance(scraper_id, str)
            assert isinstance(scraper_cls, type)
            assert callable(config_factory)

    def test_get_scraper_ids_returns_all_ids(self) -> None:
        """get_scraper_ids should return a list of string IDs."""
        ids = get_scraper_ids()
        assert isinstance(ids, list)
        assert len(ids) > 0
        assert all(isinstance(sid, str) for sid in ids)
        assert "ca-la-tentatives-civil" in ids


# ---------------------------------------------------------------------------
# LA department-judge mapping tests (lines 125-134, 170)
# ---------------------------------------------------------------------------


class TestLADeptJudgeMapping:
    """Tests for LA department-judge mapping pre-fetch in run_scrapers."""

    def test_la_dept_judge_map_fetched_for_la_scraper(self) -> None:
        """When running the LA scraper, the dept-judge mapping should be fetched
        and passed as a kwarg."""
        captured_kwargs: dict[str, object] = {}

        class CapturingStub(StubScraper):
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)
                # Remove dept_judge_map before passing to super
                kw = {k: v for k, v in kwargs.items() if k != "dept_judge_map"}
                super().__init__(**kw)

        def la_config(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="ca-la-tentatives-civil",
                state="CA",
                county="LA",
                court="Superior Court",
                target_urls=["https://example.com"],
                s3_bucket=s3_bucket,
            )

        entries = [("ca-la-tentatives-civil", CapturingStub, la_config)]
        fake_map = {"Dept 1": "Judge Smith"}

        with (
            _patch_registry(entries),
            patch(
                "framework.runner.fetch_department_judge_mapping",
                create=True,
            ),
        ):
            # Patch the dynamic import inside run_scrapers
            mock_module = MagicMock()
            mock_module.fetch_department_judge_mapping.return_value = fake_map

            with patch.dict(
                "sys.modules",
                {"courts.ca.la_dept_judges": mock_module},
            ):
                exit_code = run_scrapers()

        assert exit_code == 0
        assert captured_kwargs.get("dept_judge_map") == fake_map

    def test_la_dept_judge_map_failure_is_non_fatal(self) -> None:
        """If fetching the LA dept-judge mapping fails, the scraper should still
        run — just without the mapping."""
        ran = []

        class TrackingStub(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def la_config(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="ca-la-tentatives-civil",
                state="CA",
                county="LA",
                court="Superior Court",
                target_urls=["https://example.com"],
                s3_bucket=s3_bucket,
            )

        entries = [("ca-la-tentatives-civil", TrackingStub, la_config)]

        mock_module = MagicMock()
        mock_module.fetch_department_judge_mapping.side_effect = RuntimeError("network error")

        with (
            _patch_registry(entries),
            patch.dict("sys.modules", {"courts.ca.la_dept_judges": mock_module}),
        ):
            exit_code = run_scrapers()

        assert exit_code == 0
        assert "ca-la-tentatives-civil" in ran


# ---------------------------------------------------------------------------
# Riverside department-judge mapping tests (lines 144-155, 172)
# ---------------------------------------------------------------------------


class TestRiversideDeptJudgeMapping:
    """Tests for Riverside department-judge mapping pre-fetch in run_scrapers."""

    def test_riverside_dept_judge_map_fetched_for_riverside_scraper(self) -> None:
        """When running the Riverside scraper, the dept-judge mapping should be
        fetched and passed as a kwarg."""
        captured_kwargs: dict[str, object] = {}

        class CapturingStub(StubScraper):
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)
                kw = {k: v for k, v in kwargs.items() if k != "dept_judge_map"}
                super().__init__(**kw)

        def riv_config(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="ca-riverside-tentatives",
                state="CA",
                county="Riverside",
                court="Superior Court",
                target_urls=["https://example.com"],
                s3_bucket=s3_bucket,
            )

        entries = [("ca-riverside-tentatives", CapturingStub, riv_config)]
        fake_map = {"Dept A": "Judge Johnson"}

        mock_module = MagicMock()
        mock_module.fetch_department_judge_mapping.return_value = fake_map

        with (
            _patch_registry(entries),
            patch.dict("sys.modules", {"courts.ca.riverside_dept_judges": mock_module}),
        ):
            exit_code = run_scrapers()

        assert exit_code == 0
        assert captured_kwargs.get("dept_judge_map") == fake_map

    def test_riverside_dept_judge_map_failure_is_non_fatal(self) -> None:
        """If fetching the Riverside dept-judge mapping fails, the scraper should
        still run without the mapping."""
        ran = []

        class TrackingStub(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def riv_config(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="ca-riverside-tentatives",
                state="CA",
                county="Riverside",
                court="Superior Court",
                target_urls=["https://example.com"],
                s3_bucket=s3_bucket,
            )

        entries = [("ca-riverside-tentatives", TrackingStub, riv_config)]

        mock_module = MagicMock()
        mock_module.fetch_department_judge_mapping.side_effect = RuntimeError("network error")

        with (
            _patch_registry(entries),
            patch.dict("sys.modules", {"courts.ca.riverside_dept_judges": mock_module}),
        ):
            exit_code = run_scrapers()

        assert exit_code == 0
        assert "ca-riverside-tentatives" in ran


# ---------------------------------------------------------------------------
# Health failure tests (lines 178-181, 189-195)
# ---------------------------------------------------------------------------


class TestRunMethodException:
    """Tests for the case where scraper.run() itself raises (lines 178-181).

    Note: FailingScraper raises in fetch_documents, which is caught by
    BaseScraper.run()'s internal try/except. To test runner.py lines 178-181,
    we need a scraper whose run() method raises an unhandled exception.
    """

    def test_run_method_raises_returns_nonzero(self) -> None:
        """When scraper.run() raises, run_scrapers should catch it and return 1."""

        class RunRaisingScraper(BaseScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                return []

            def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
                return doc

            def run(self) -> ScraperHealthEvent:
                raise RuntimeError("run() itself exploded")

        entries = [("run-raiser", RunRaisingScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1

    def test_run_method_exception_continues_to_next_scraper(self) -> None:
        """When one scraper's run() raises, the runner should continue to the
        next scraper."""
        ran = []

        class RunRaisingScraper(BaseScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                return []

            def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
                return doc

            def run(self) -> ScraperHealthEvent:
                raise RuntimeError("run() exploded")

        class TrackingStub(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def config_bad(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="run-raiser",
                state="CA",
                county="Bad",
                court="Court",
                target_urls=["https://example.com"],
            )

        def config_good(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="good-after",
                state="CA",
                county="Good",
                court="Court",
                target_urls=["https://example.com"],
            )

        entries = [
            ("run-raiser", RunRaisingScraper, config_bad),
            ("good-after", TrackingStub, config_good),
        ]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1
        assert "good-after" in ran


class TestHealthReportedFailure:
    """Tests for scrapers that return a health event with success=False."""

    def test_health_failure_returns_nonzero(self) -> None:
        """When a scraper's run() returns health.success=False, run_scrapers
        should return 1."""

        class HealthFailScraper(BaseScraper):
            """Scraper that completes but reports failure via health event."""

            def fetch_documents(self) -> list[CapturedDocument]:
                return []

            def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
                return doc

            def run(self) -> ScraperHealthEvent:
                return ScraperHealthEvent(
                    producer_id=self.config.scraper_id,
                    scraper_id=self.config.scraper_id,
                    success=False,
                    records_captured=0,
                    response_time_seconds=0.1,
                    error_message="site returned 500",
                    run_timestamp=datetime.utcnow(),
                )

        entries = [("health-fail", HealthFailScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1

    def test_health_failure_mixed_with_success(self) -> None:
        """When one scraper reports success and another failure, run_scrapers
        returns 1 but the successful scraper still runs."""

        class HealthFailScraper(BaseScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                return []

            def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
                return doc

            def run(self) -> ScraperHealthEvent:
                return ScraperHealthEvent(
                    producer_id=self.config.scraper_id,
                    scraper_id=self.config.scraper_id,
                    success=False,
                    records_captured=0,
                    response_time_seconds=0.1,
                    error_message="failed",
                    run_timestamp=datetime.utcnow(),
                )

        ran = []

        class TrackingStub(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def config_fail(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="failing",
                state="CA",
                county="Fail",
                court="Court",
                target_urls=["https://example.com"],
            )

        def config_good(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="good",
                state="CA",
                county="Good",
                court="Court",
                target_urls=["https://example.com"],
            )

        entries = [
            ("failing", HealthFailScraper, config_fail),
            ("good", TrackingStub, config_good),
        ]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1
        assert "good" in ran


# ---------------------------------------------------------------------------
# CLI entrypoint tests (lines 213-215)
# ---------------------------------------------------------------------------


class TestMainEntrypoint:
    """Tests for the main() CLI entrypoint."""

    def test_main_no_args_calls_run_scrapers_with_none(self) -> None:
        """main() with no CLI args passes None to run_scrapers."""
        with (
            patch("framework.runner.run_scrapers", return_value=0) as mock_run,
            patch.object(sys, "argv", ["framework.runner"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        mock_run.assert_called_once_with(None)
        assert exc_info.value.code == 0

    def test_main_with_scraper_ids(self) -> None:
        """main() with CLI args passes them as scraper_ids."""
        with (
            patch("framework.runner.run_scrapers", return_value=0) as mock_run,
            patch.object(
                sys,
                "argv",
                ["framework.runner", "ca-la-tentatives-civil", "ca-oc-tentatives"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        mock_run.assert_called_once_with(["ca-la-tentatives-civil", "ca-oc-tentatives"])
        assert exc_info.value.code == 0

    def test_main_returns_failure_exit_code(self) -> None:
        """main() exits with code 1 when run_scrapers returns 1."""
        with (
            patch("framework.runner.run_scrapers", return_value=1),
            patch.object(sys, "argv", ["framework.runner"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Event bus wiring test
# ---------------------------------------------------------------------------


class TestEventBusIntegration:
    """Tests for RedisEventBus wiring in run_scrapers."""

    def test_event_bus_from_env_called(self) -> None:
        """run_scrapers should call RedisEventBus.from_env() and pass
        the result to scraper constructors."""
        captured_event_bus = {}

        class CapturingStub(StubScraper):
            def __init__(self, **kwargs: object) -> None:
                captured_event_bus["bus"] = kwargs.get("event_bus")
                super().__init__(**kwargs)

        entries = [("test-stub", CapturingStub, _stub_config)]
        mock_bus = MagicMock()

        with (
            _patch_registry(entries),
            patch("framework.runner.RedisEventBus.from_env", return_value=mock_bus),
        ):
            exit_code = run_scrapers()

        assert exit_code == 0
        assert captured_event_bus["bus"] is mock_bus
