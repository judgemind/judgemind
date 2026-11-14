"""The worker's extraction mode (#4953).

A deployed worker (the ECS service, the reingest and rebuild oneshots) runs
in ``required`` mode, the default: it builds its LLM extractors at startup
and refuses to start when one cannot be built (a missing or blank secret, a
client init error).  Before, it logged a warning and ran as if no extractor
were configured, so a reingest wrote the whole document as a ruling next to
the key's split children.  ``JUDGEMIND_EXTRACTION=optional`` is the explicit
opt-in for runs without LLM keys.

The split-time half (a missing extractor writes nothing) is tested in
``test_oc_fused_tail_4714.py`` (``-k extractor_unavailable``).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from framework.llm_extractor import LlmExtractor
from ingestion.worker import (
    EXTRACTION_MODE_ENV,
    ExtractorUnavailableError,
    IngestionWorker,
    check_extraction_ready,
    extraction_mode,
)


def _make_worker() -> IngestionWorker:
    return IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )


@pytest.mark.parametrize(
    ("value", "mode"),
    [(None, "required"), ("", "required"), ("required", "required"), (" Optional ", "optional")],
)
def test_extraction_mode_reads_the_env(
    monkeypatch: pytest.MonkeyPatch, value: str | None, mode: str
) -> None:
    if value is None:
        monkeypatch.delenv(EXTRACTION_MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(EXTRACTION_MODE_ENV, value)
    assert extraction_mode() == mode


def test_extraction_mode_rejects_an_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently pick a mode."""
    monkeypatch.setenv(EXTRACTION_MODE_ENV, "none")
    with pytest.raises(ValueError, match="not a known extraction mode"):
        extraction_mode()
    with pytest.raises(ValueError):
        _make_worker()


def test_required_mode_builds_the_extractors_at_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(EXTRACTION_MODE_ENV, raising=False)
    with (
        patch("ingestion.worker.LlmExtractor") as extractor_cls,
        caplog.at_level(logging.INFO, logger="ingestion.worker"),
    ):
        worker = _make_worker()

    assert worker._extraction_required is True
    providers = [c.kwargs.get("provider", "anthropic") for c in extractor_cls.call_args_list]
    assert providers == ["anthropic", "google"]
    # The startup check fills the caches the splits use.
    assert worker._framework_extractor is extractor_cls.return_value
    assert worker._get_multimodal_extractor(max_output_tokens=4096) is extractor_cls.return_value
    assert extractor_cls.call_count == 2
    mode_logs = [
        r
        for r in caplog.records
        if getattr(r, "telemetry_event", None) == "ingestion.extraction_mode"
    ]
    assert [r.extraction_mode for r in mode_logs] == ["required"]


@pytest.mark.parametrize("missing", ["anthropic", "google"])
def test_required_mode_refuses_to_start_without_an_extractor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing: str
) -> None:
    """A secret that is missing (or a client that will not build) fails the
    worker at startup, loudly, naming the extractor and the init error."""
    monkeypatch.setenv(EXTRACTION_MODE_ENV, "required")

    def build(**kwargs: object) -> MagicMock:
        if kwargs.get("provider", "anthropic") == missing:
            raise ValueError(f"no {missing} key")
        return MagicMock()

    label = "framework" if missing == "anthropic" else "multimodal"
    with (
        patch("ingestion.worker.LlmExtractor", side_effect=build),
        caplog.at_level(logging.WARNING, logger="ingestion.worker"),
        pytest.raises(
            ExtractorUnavailableError,
            match=f"extractor unavailable: {label}: ValueError: no {missing} key",
        ),
    ):
        _make_worker()

    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1
    assert critical[0].telemetry_event == "ingestion.extractor_unavailable"
    assert critical[0].missing == [label]


def test_optional_mode_builds_no_extractor_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXTRACTION_MODE_ENV, "optional")
    with patch("ingestion.worker.LlmExtractor", side_effect=ValueError("no key")) as cls:
        worker = _make_worker()
    assert worker._extraction_required is False
    cls.assert_not_called()


def test_check_extraction_ready_in_required_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scripts' preflight (``rebuild_db.py`` runs it before its reset)."""
    monkeypatch.setenv(EXTRACTION_MODE_ENV, "required")
    with patch("ingestion.worker.LlmExtractor") as cls:
        assert check_extraction_ready() == "required"
    assert [c.kwargs.get("provider", "anthropic") for c in cls.call_args_list] == [
        "anthropic",
        "google",
    ]

    def build(**kwargs: object) -> MagicMock:
        if kwargs.get("provider") == "google":
            raise ValueError("no google key")
        return MagicMock()

    with (
        patch("ingestion.worker.LlmExtractor", side_effect=build),
        pytest.raises(
            ExtractorUnavailableError,
            match="multimodal: ValueError: no google key.*JUDGEMIND_EXTRACTION=optional",
        ),
    ):
        check_extraction_ready()


def test_check_extraction_ready_in_optional_mode_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(EXTRACTION_MODE_ENV, "optional")
    with patch("ingestion.worker.LlmExtractor", side_effect=ValueError("no key")) as cls:
        assert check_extraction_ready() == "optional"
    cls.assert_not_called()


def test_rebuild_refuses_before_its_reset_without_an_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``rebuild_db.py --reset`` with a missing LLM key must stop before it
    truncates the derived tables: every document's worker would refuse,
    leaving them empty (#4953)."""
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    import rebuild_db

    monkeypatch.setenv(EXTRACTION_MODE_ENV, "required")
    monkeypatch.setenv("DATABASE_URL", "postgres://test")
    monkeypatch.setattr(sys, "argv", ["rebuild_db.py", "--reset"])
    with (
        patch("ingestion.worker.LlmExtractor", side_effect=ValueError("no key")),
        patch("psycopg.connect") as connect,
        patch("rebuild_db.reset_derived_tables") as reset,
        pytest.raises(ExtractorUnavailableError),
    ):
        rebuild_db.main()
    reset.assert_not_called()
    connect.assert_not_called()


def test_blank_anthropic_key_is_a_missing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic SDK accepts a blank key and fails only on the first
    call, which the worker would see as a failed extraction per document
    instead of a startup failure (#4953)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is blank"):
        LlmExtractor()
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is blank"):
        LlmExtractor(api_key="")


def test_blank_google_key_is_a_missing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", " \n")
    with pytest.raises(ValueError, match="No Google API key"):
        LlmExtractor(provider="google")
