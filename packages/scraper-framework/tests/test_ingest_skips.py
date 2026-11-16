"""Unit tests for ingestion.ingest_skips and the worker's empty-split reason (#5015)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ingestion.ingest_skips import (
    EXTRACTION_SKIP_REASONS,
    SKIP_NO_RULING_TEXT,
    SKIP_REASONS,
    SKIP_SANTA_CLARA_PLACEHOLDER,
    record_ingest_skip,
)
from ingestion.worker import (
    _empty_split_skip_reason,
    _extraction_skip_verdict,
    _pdf_extractor_identity,
    _SplitBatch,
    _writes_nothing,
)
from validation.skip_versions import EXTRACTION_SKIP_RULE_VERSION, stale_skip_reason


def _conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    return conn, cur


def test_skip_reason_record_upserts_under_a_savepoint() -> None:
    conn, cur = _conn()
    ok = record_ingest_skip(
        conn,
        s3_key="ca/santa_clara/superior_court/raw/abc.pdf",
        reason=SKIP_SANTA_CLARA_PLACEHOLDER,
        county="Santa Clara",
        scraper_id="ca-sc-tentatives-civil",
        document_id="doc-1",
        detail={"removed_rows": 2},
    )
    assert ok is True
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert sqls[0] == "SAVEPOINT ingest_skip"
    assert "INSERT INTO telemetry.ingest_skips" in sqls[1]
    assert "ON CONFLICT (s3_key) DO UPDATE" in sqls[1]
    assert sqls[2] == "RELEASE SAVEPOINT ingest_skip"
    params = cur.execute.call_args_list[1].args[1]
    assert params[:5] == (
        "ca/santa_clara/superior_court/raw/abc.pdf",
        "santa_clara_placeholder",
        "Santa Clara",
        "ca-sc-tentatives-civil",
        "doc-1",
    )
    assert json.loads(params[5]) == {"removed_rows": 2}
    # The caller owns the transaction.
    conn.commit.assert_not_called()


def test_skip_reason_record_without_s3_key_writes_nothing() -> None:
    conn, cur = _conn()
    assert record_ingest_skip(conn, s3_key=None, reason=SKIP_NO_RULING_TEXT) is False
    assert record_ingest_skip(conn, s3_key="", reason=SKIP_NO_RULING_TEXT) is False
    cur.execute.assert_not_called()


def test_skip_reason_record_unknown_reason_is_not_written() -> None:
    conn, cur = _conn()
    assert record_ingest_skip(conn, s3_key="k", reason="made_up") is False
    cur.execute.assert_not_called()


def test_skip_reason_record_failure_rolls_back_to_savepoint_and_never_raises() -> None:
    conn, cur = _conn()

    def execute(sql: str, *_a: object) -> None:
        if "INSERT" in sql:
            raise RuntimeError('relation "telemetry.ingest_skips" does not exist')

    cur.execute.side_effect = execute
    assert record_ingest_skip(conn, s3_key="k", reason=SKIP_NO_RULING_TEXT) is False
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert sqls[-1] == "ROLLBACK TO SAVEPOINT ingest_skip"


def test_skip_reason_record_cursor_failure_never_raises() -> None:
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("connection closed")
    assert record_ingest_skip(conn, s3_key="k", reason=SKIP_NO_RULING_TEXT) is False


def test_skip_reasons_match_the_migration_comment() -> None:
    assert SKIP_REASONS == {
        "governor_bio_route",
        "santa_clara_placeholder",
        "zero_rulings_no_text",
        "no_extractable_text",
        "no_ruling_text",
        "raw_missing",
    }


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        pytest.param(
            _SplitBatch("p", no_rulings_reason="zero_rulings_no_text"),
            "zero_rulings_no_text",
            id="no-rulings-reason",
        ),
        pytest.param(_SplitBatch("p", textless_rows=3), "no_ruling_text", id="textless-only"),
        pytest.param(_SplitBatch("p"), None, id="children-rejected-elsewhere"),
        pytest.param(_SplitBatch("p", textless_rows=1, keep_ids={"c"}), None, id="kept-child"),
        pytest.param(
            _SplitBatch("p", textless_rows=1, children=[MagicMock()]), None, id="written-child"
        ),
        pytest.param(
            _SplitBatch("p", textless_rows=1, pre_split=MagicMock()), None, id="pre-split"
        ),
        # Rows with text were found and rejected: not "all textless" (#5094).
        pytest.param(
            _SplitBatch("p", textless_rows=1, rejections=[{"reason": "validation"}]),
            None,
            id="textless-and-rejected",
        ),
    ],
)
def test_skip_reason_for_empty_split(batch: _SplitBatch, expected: str | None) -> None:
    assert _empty_split_skip_reason(batch) == expected


@pytest.mark.parametrize("reason", sorted(EXTRACTION_SKIP_REASONS))
def test_extraction_skip_is_stamped_with_the_rule_version(reason: str) -> None:
    """#5094: an extraction verdict carries the rule version that produced
    it, so the raw-key reconciler can re-judge it."""
    conn, cur = _conn()
    assert record_ingest_skip(conn, s3_key="k", reason=reason, detail={"held_rulings": 0})
    params = cur.execute.call_args_list[1].args[1]
    assert json.loads(params[5]) == {
        "held_rulings": 0,
        "rule_version": EXTRACTION_SKIP_RULE_VERSION,
    }


def test_deterministic_skip_carries_no_rule_version() -> None:
    conn, cur = _conn()
    assert record_ingest_skip(conn, s3_key="k", reason=SKIP_SANTA_CLARA_PLACEHOLDER)
    assert json.loads(cur.execute.call_args_list[1].args[1][5]) == {}


@pytest.mark.parametrize(
    ("batch", "writes_nothing", "verdict"),
    [
        pytest.param(_SplitBatch("p", textless_rows=2), True, "no_ruling_text", id="textless"),
        pytest.param(
            _SplitBatch("p", no_rulings_reason="zero_rulings_no_text"),
            True,
            "zero_rulings_no_text",
            id="zero-rulings-no-text",
        ),
        pytest.param(
            _SplitBatch("p", no_rulings_reason="santa_clara_placeholder"),
            False,
            None,
            id="deterministic-placeholder",
        ),
        pytest.param(
            _SplitBatch("p", whole_document=True, no_rulings_verdict=True),
            False,
            None,
            id="no-rulings-verdict",
        ),
        pytest.param(_SplitBatch("p"), True, None, id="all-rejected"),
    ],
)
def test_extraction_verdicts_write_nothing(
    batch: _SplitBatch, writes_nothing: bool, verdict: str | None
) -> None:
    """#5094: an extraction's skip verdict is refused over held rulings like
    a split whose every child was rejected; a deterministic one is not."""
    assert _writes_nothing(batch) is writes_nothing
    assert _extraction_skip_verdict(batch) == verdict


def test_pdf_extractor_identity_reads_the_cache_namespace() -> None:
    extractor = MagicMock()
    extractor.pdf_cache_namespace = "google-gemini/prompt-abc"
    assert _pdf_extractor_identity(extractor) == "google-gemini/prompt-abc"
    # A test double without the property (a MagicMock attribute) has none.
    assert _pdf_extractor_identity(MagicMock()) is None
    assert _pdf_extractor_identity(None) is None


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    [
        pytest.param("no_ruling_text", None, "rule_version", id="no-detail"),
        pytest.param("no_ruling_text", {}, "rule_version", id="pre-5094-row"),
        pytest.param("zero_rulings_no_text", {"rule_version": 1}, "rule_version", id="v1"),
        pytest.param("no_ruling_text", {"rule_version": "x"}, "rule_version", id="garbled"),
        pytest.param(
            "no_ruling_text",
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "held_rulings": 0},
            None,
            id="current-fresh-key",
        ),
        pytest.param(
            "no_ruling_text",
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "held_rulings": 2},
            "removed_rulings",
            id="removed-rulings",
        ),
        pytest.param(
            "no_ruling_text",
            {
                "rule_version": EXTRACTION_SKIP_RULE_VERSION,
                "held_rulings": 2,
                "allowed_removal": True,
            },
            None,
            id="allowed-removal",
        ),
        pytest.param("santa_clara_placeholder", {}, None, id="deterministic"),
        pytest.param(None, {}, None, id="no-skip"),
    ],
)
def test_stale_skip_reason(reason: str | None, detail: object, expected: str | None) -> None:
    assert stale_skip_reason(reason, detail) == expected  # type: ignore[arg-type]


def test_pdf_cache_namespace_names_provider_model_and_prompt() -> None:
    from framework.llm_extractor import PDF_PER_PAGE_PROMPT, LlmExtractor, _prompt_hash

    extractor = LlmExtractor.__new__(LlmExtractor)
    extractor._provider = "google"
    extractor._model = "gemini-2.5-flash-lite"
    assert extractor.pdf_cache_namespace == (
        f"google-gemini-2.5-flash-lite/prompt-{_prompt_hash(PDF_PER_PAGE_PROMPT)[:16]}"
    )


OLD_EXTRACTOR = "google-old-model/prompt-x"
NEW_EXTRACTOR = "google-gemini-2.5-flash-lite/prompt-new"


@pytest.mark.parametrize(
    ("detail", "current", "expected"),
    [
        pytest.param(
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "extractor": OLD_EXTRACTOR},
            NEW_EXTRACTOR,
            "extractor",
            id="other-extractor",
        ),
        pytest.param(
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "extractor": NEW_EXTRACTOR},
            NEW_EXTRACTOR,
            None,
            id="same-extractor",
        ),
        pytest.param(
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION},
            NEW_EXTRACTOR,
            None,
            id="row-without-extractor",
        ),
        pytest.param(
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "extractor": OLD_EXTRACTOR},
            None,
            None,
            id="no-current-extractor",
        ),
        pytest.param(
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "extractor": OLD_EXTRACTOR},
            "",
            None,
            id="empty-current-extractor",
        ),
        pytest.param(
            {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "extractor": ""},
            NEW_EXTRACTOR,
            None,
            id="empty-row-extractor",
        ),
        pytest.param(
            {"rule_version": 2, "extractor": NEW_EXTRACTOR},
            NEW_EXTRACTOR,
            "rule_version",
            id="version-2-row",
        ),
    ],
)
def test_stale_skip_reason_compares_the_recorded_extractor(
    detail: dict[str, object], current: str | None, expected: str | None
) -> None:
    """#5134: a model or per-page-prompt change re-judges a skip without a
    version bump, because the recorded extractor identity differs."""
    assert stale_skip_reason("no_ruling_text", detail, current) == expected


def test_skip_rule_version_is_bumped_for_the_ocr_tile_fix() -> None:
    """#5134: #5115 (#5098) changed the OCR tile split; version 2 skips are stale."""
    assert EXTRACTION_SKIP_RULE_VERSION >= 3
    assert stale_skip_reason("zero_rulings_no_text", {"rule_version": 2}) == "rule_version"


def test_current_pdf_cache_namespace_matches_the_worker_multimodal_extractor() -> None:
    """The reconciler's current extractor equals what the worker records."""
    from framework.llm_extractor import (
        MULTIMODAL_PDF_MODEL,
        MULTIMODAL_PDF_PROVIDER,
        LlmExtractor,
        current_pdf_cache_namespace,
        pdf_cache_namespace,
    )

    extractor = LlmExtractor.__new__(LlmExtractor)
    extractor._provider = MULTIMODAL_PDF_PROVIDER
    extractor._model = MULTIMODAL_PDF_MODEL
    assert (MULTIMODAL_PDF_PROVIDER, MULTIMODAL_PDF_MODEL) == ("google", "gemini-2.5-flash-lite")
    assert current_pdf_cache_namespace() == extractor.pdf_cache_namespace
    assert pdf_cache_namespace("p", "m").startswith("p-m/prompt-")


def test_worker_builds_the_multimodal_extractor_from_the_shared_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both multimodal call sites build the extractor the reconciler names."""
    import ingestion.worker as worker_mod
    from framework.llm_extractor import (
        MULTIMODAL_PDF_MODEL,
        MULTIMODAL_PDF_PROVIDER,
        pdf_cache_namespace,
    )

    calls: list[dict[str, object]] = []

    class _FakeExtractor:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            self.pdf_cache_namespace = pdf_cache_namespace(
                str(kwargs["provider"]), str(kwargs["model"])
            )

    monkeypatch.setattr(worker_mod, "LlmExtractor", _FakeExtractor)
    worker = worker_mod.IngestionWorker.__new__(worker_mod.IngestionWorker)
    worker._multimodal_extractor = None
    worker._multimodal_extractors = {}
    worker._extractor_init_errors = {}
    worker._bust_llm_cache = False
    worker._get_multimodal_extractor(max_output_tokens=32768)
    worker._get_multimodal_extractor()
    assert [(c["provider"], c["model"]) for c in calls] == [
        (MULTIMODAL_PDF_PROVIDER, MULTIMODAL_PDF_MODEL)
    ] * 2


@pytest.mark.parametrize(
    ("method", "text", "expected"),
    [
        ("multimodal", "", True),
        ("multimodal", "  \n", True),
        ("multimodal", None, True),
        ("multimodal", "GRANTED.", False),
        ("text", "", False),
    ],
)
def test_textless_multimodal_row(method: str, text: str | None, expected: bool) -> None:
    from ingestion.worker import _is_textless_multimodal_row

    assert _is_textless_multimodal_row(method, text) is expected


@pytest.mark.parametrize(
    ("method", "rulings", "text", "expected"),
    [
        ("multimodal", [], "", True),
        ("multimodal", [], None, True),
        ("multimodal", None, "", False),
        ("multimodal", [], "page text", False),
        ("text", [], "", False),
    ],
)
def test_zero_rulings_no_text_decision(
    method: str, rulings: list[object] | None, text: str | None, expected: bool
) -> None:
    from ingestion.worker import _is_zero_rulings_no_text

    assert _is_zero_rulings_no_text(method, rulings, text) is expected
