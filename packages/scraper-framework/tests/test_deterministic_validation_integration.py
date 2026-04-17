"""Integration tests for deterministic validation in the ingestion worker.

Verifies that the IngestionWorker correctly runs deterministic validation
rules between enrichment and DB write, handles fail/flag/pass results,
and logs validation results to the DB.

Covers the three spotcheck findings from issue #2329:
- LA ruling with raw HTML (no_html_in_ruling_text -> fail)
- SD ruling with wrong hearing date (hearing_date_in_range -> fail)
- Fresno ruling with concatenated titles (no_multiple_adversarial_patterns -> flag)

Also covers document-level validation (#2350):
- Duplicate ruling text lengths across split events (no_duplicate_ruling_text -> flag)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> dict:
    """Return a minimal valid DocumentCapturedEvent payload."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "scraper_id": "ca-la-tentatives-civil",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": "https://www.lacourt.org/tentativerulings/1",
        "content_format": "html",
        "content_hash": "abc123",
        "s3_key": "ca/los_angeles/superior_court/raw/abc123.html",
        "s3_bucket": "judgemind-document-archive-dev",
        "case_number": "23STCV12345",
        "department": "Dept. 1",
        "judge_name": "Smith, John A.",
        "ruling_text": "The motion for summary judgment is GRANTED. " * 5,
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (mock_conn, mock_cur) pair."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def _make_worker() -> tuple[IngestionWorker, MagicMock]:
    """Return a worker with mocked dependencies.

    The LLM client is set to None to disable per-field LLM extraction.
    LLM validation gate is also disabled so we only test deterministic rules.
    """
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False

    with patch.dict("os.environ", {}):
        with patch("ingestion.worker.create_llm_client") as mock_create:
            mock_create.return_value = None
            worker = IngestionWorker(
                redis_client=redis_mock,
                pg_dsn="postgresql://localhost/test",
                opensearch_client=os_mock,
                s3_client=s3_mock,
                archive_bucket="test-bucket",
                llm_client=None,
            )

    return worker, os_mock


# Mocks to prevent LLM extraction and document splitting paths
_SPLIT_MOCK = "ingestion.worker.IngestionWorker._llm_split_document"
_EXTRACT_LLM_MOCK = "ingestion.worker.extract_fields_llm"


# ---------------------------------------------------------------------------
# Tests: deterministic validation — pass (normal document)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_pass_allows_db_write(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """A normal document passes all deterministic rules and is written to DB."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    worker.process_event(event)

    # Document was committed to DB (pass = no blocking)
    mock_conn.commit.assert_called()
    # No deterministic validation result logged (only logged for flag/fail)
    # The insert_validation_result may not be called at all for pass
    # (it depends on whether LLM validation is enabled)


# ---------------------------------------------------------------------------
# Tests: deterministic validation — fail (raw HTML)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_html_skips_db_write(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """LA ruling 65932ec6 — raw HTML triggers no_html_in_ruling_text fail, skipping DB write."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event(
        ruling_text="<html><head><title>LASC</title></head><body><div>ruling</div></body></html>",
    )
    worker.process_event(event)

    # insert_validation_result should be called with a fail result
    mock_insert_validation.assert_called_once()
    call_kwargs = mock_insert_validation.call_args
    result = call_kwargs.kwargs["result"]
    assert result.result == "fail"
    assert result.model == "deterministic"
    assert "raw HTML detected" in (result.reason or "")

    # OpenSearch indexing should NOT have happened (write path was skipped)
    os_mock.index.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: deterministic validation — fail (wrong hearing date)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_wrong_date_skips_db_write(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """SD ruling 5eac1c2d — hearing_date=2003 triggers hearing_date_in_range fail."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event(
        county="San Diego",
        hearing_date="2003-07-15",
        capture_timestamp="2026-03-04T23:00:00",
    )
    worker.process_event(event)

    # insert_validation_result should be called with a fail result
    mock_insert_validation.assert_called_once()
    result = mock_insert_validation.call_args.kwargs["result"]
    assert result.result == "fail"
    assert result.model == "deterministic"
    assert "exceeds" in (result.reason or "")
    assert "2003-07-15" in (result.reason or "")

    # OpenSearch indexing should NOT have happened
    os_mock.index.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: deterministic validation — flag (concatenated titles)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_concatenated_title_still_writes(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Fresno ruling 4647a509 — concatenated titles trigger a flag but still write to DB."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (deterministic flag logging)
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    concatenated_title = (
        "Alpha v. Beta; Gamma v. Delta; Epsilon v. Zeta; Eta v. Theta; Iota v. Kappa"
    )
    event = _make_event(
        county="Fresno",
        case_title=concatenated_title,
    )
    worker.process_event(event)

    # Document was still committed to DB (flag doesn't block)
    assert mock_conn.commit.call_count >= 1

    # insert_validation_result should be called with a flag result
    mock_insert_validation.assert_called()
    # Find the deterministic validation call
    det_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result") and c.kwargs["result"].model == "deterministic"
    ]
    assert len(det_calls) >= 1
    det_result = det_calls[0].kwargs["result"]
    assert det_result.result == "flag"
    assert "multi-case contamination" in (det_result.reason or "")


# ---------------------------------------------------------------------------
# Tests: deterministic validation — flag (multiple adversarial patterns, #2398)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_multiple_adversarial_patterns_issue_2398_example(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Issue #2398 example title triggers the per-ruling adversarial-pattern flag.

    Uses the Wang/NetEase example cited in the issue body:
    ``"Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando Contreras
    et al. Jin Yin, et al vs Xiaoxiao Lu, et al."`` (three ``v.``/``vs`` tokens).

    The issue's other cited title (TAYLOR VS. AMAZON with embedded case number
    ``MSC21-02349``) is intentionally not used here because ``clean_case_title``
    in ``extract.py`` reduces it to a single ``Plaintiff v. Defendant`` pair
    upstream of the deterministic rule — defense-in-depth catches that variant
    before the rule runs. The Wang title has no embedded case number, passes
    ``is_plausible_case_title``, and survives to the deterministic rule intact.

    Acceptance criterion: the rule fires in the worker's per-ruling validation
    pass with an ``insert_validation_result`` call, and the document is still
    committed to the DB (flag does not block writes).
    """
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (deterministic flag logging)
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    contaminated_title = (
        "Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando Contreras "
        "et al. Jin Yin, et al vs Xiaoxiao Lu, et al."
    )
    event = _make_event(
        county="Contra Costa",
        case_title=contaminated_title,
    )
    worker.process_event(event)

    # Document was still committed to DB (flag doesn't block)
    assert mock_conn.commit.call_count >= 1

    # insert_validation_result should have been called for the deterministic flag.
    mock_insert_validation.assert_called()
    det_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result") and c.kwargs["result"].model == "deterministic"
    ]
    assert len(det_calls) >= 1
    det_result = det_calls[0].kwargs["result"]
    assert det_result.result == "flag"
    assert "multi-case contamination" in (det_result.reason or "")
    # The reason should report 3 adversarial patterns for this Wang/NetEase title.
    assert "3" in (det_result.reason or "")


# ---------------------------------------------------------------------------
# Tests: deterministic validation — fail (empty ruling text, #2646)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_document_and_ruling")
@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_empty_ruling_text_rejected(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
    mock_insert_doc_ruling: MagicMock,
) -> None:
    """Empty ruling text triggers a fail and skips the DB write (#2646).

    Before #2646 this test asserted "flag but still writes" — the rule
    was ``flag`` and the worker wrote the NULL-text row anyway, polluting
    ``derived.rulings`` (19 NULL rows accumulated for Santa Clara).  The
    rule is now ``fail`` and the worker skips the insert.
    """
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # upsert_court + upsert_case run before deterministic validation fires.
    # insert_document (and therefore its (True,) fetch) never runs because
    # the fail path returns before insert_document_and_ruling.
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
    ]
    mock_cur.rowcount = 1

    event = _make_event(ruling_text="")
    worker.process_event(event)

    # insert_document_and_ruling must NOT have been called — empty text
    # now fails validation and the worker returns before the DB write.
    mock_insert_doc_ruling.assert_not_called()

    # A deterministic fail result with the empty-text telemetry marker
    # was logged instead.
    fail_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and c.kwargs["result"].result == "fail"
        and "ruling_text is null or empty" in (c.kwargs["result"].reason or "")
    ]
    assert len(fail_calls) == 1
    assert "data_quality.ruling_empty_text_dropped" in (fail_calls[0].kwargs["result"].reason or "")


# ---------------------------------------------------------------------------
# Tests: deterministic validation — fail DB logging error does not crash
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_db_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If insert_validation_result raises on a fail, the worker still returns cleanly."""
    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event(
        ruling_text="<html><body>raw html</body></html>",
    )
    # Should not raise despite DB error during validation result insert
    worker.process_event(event)

    # OpenSearch indexing should NOT have happened (fail path)
    os_mock.index.assert_not_called()

    # Rollback must be called after the insert failure so that any aborted
    # transaction state is cleared on the shared DB connection (#2385).
    mock_conn.rollback.assert_called()


# ---------------------------------------------------------------------------
# Tests: deterministic validation — flag DB logging error does not crash
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_db_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If insert_validation_result raises on a flag, the worker continues to DB write."""
    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (flag logging attempt — fails)
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(ruling_text="")  # triggers flag for empty text
    # Should not raise despite DB error during flag logging
    worker.process_event(event)

    # Rollback must be called after the insert failure so that any aborted
    # transaction state is cleared before the main DB write proceeds (#2385).
    mock_conn.rollback.assert_called()


# ---------------------------------------------------------------------------
# Tests: deterministic validation — rollback() itself raising does not crash
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_rollback_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If rollback() itself raises after an insert failure on fail, still returns (#2385)."""
    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_conn.rollback.side_effect = RuntimeError("connection already closed")
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event(
        ruling_text="<html><body>raw html</body></html>",
    )
    # Should not raise even if both the insert AND the defensive rollback fail.
    worker.process_event(event)

    # OpenSearch indexing should NOT have happened (fail path)
    os_mock.index.assert_not_called()
    # rollback was attempted (and failed — but the defensive except swallowed it)
    mock_conn.rollback.assert_called()


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_rollback_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If rollback() itself raises after an insert failure on flag, still continues (#2385)."""
    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    # rollback() for the flag-logging handler will raise the first time, then
    # succeed on subsequent calls (the main DB write's except: conn.rollback()
    # re-raises if it also fails, which is undesirable for this test).
    mock_conn.rollback.side_effect = [RuntimeError("connection already closed"), None]
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (flag logging attempt — fails)
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(ruling_text="")  # triggers flag for empty text
    # Should not raise even if both the insert AND the defensive rollback fail.
    worker.process_event(event)

    # rollback was attempted (and failed — but the defensive except swallowed it)
    mock_conn.rollback.assert_called()


# ---------------------------------------------------------------------------
# Tests: document-level deterministic validation — duplicate ruling text (#2350)
# ---------------------------------------------------------------------------

_CONVERT_MOCK = "ingestion.worker.convert_extracted_rulings"
_DELETE_STALE_MOCK = "ingestion.worker.delete_stale_split_children"


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_duplicate_ruling_text_lengths(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Three split rulings with identical text lengths should flag for review (#2350)."""
    from ingestion.ruling_guards import ConvertedRuling

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # Each split event goes through process_event, which needs DB results.
    # 3 rulings x (court + case + document) = 9 fetchone calls.
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (split 0)
        ("case-uuid-1",),  # upsert_case (split 0)
        (True,),  # insert_document (split 0)
        ("court-uuid-1",),  # upsert_court (split 1)
        ("case-uuid-2",),  # upsert_case (split 1)
        (True,),  # insert_document (split 1)
        ("court-uuid-1",),  # upsert_court (split 2)
        ("case-uuid-3",),  # upsert_case (split 2)
        (True,),  # insert_document (split 2)
    ]
    mock_cur.rowcount = 1

    # Create 3 ConvertedRuling objects with identical ruling_text lengths.
    same_length_text = "The motion is GRANTED." * 10  # 220 chars
    converted = [
        ConvertedRuling(
            document_id=f"split-uuid-{i}",
            original_document_id="parent-uuid-1",
            split_index=i,
            split_count=3,
            is_multi=True,
            ruling_text=same_length_text,
            case_number=f"23STCV1234{i}",
            case_title=f"Smith{i} v. Jones{i}",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type="Motion for Summary Judgment",
            outcome="granted",
            hearing_date="2026-03-05",
        )
        for i in range(3)
    ]

    # Mock _llm_split_document internals: skip the LLM call, jump straight
    # to the conversion + validation + dispatch logic.  We patch
    # convert_extracted_rulings to return our prepared ConvertedRuling list.
    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            # Create a minimal extractor mock so _llm_split_document proceeds.
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 3)
            worker._framework_extractor = extractor_mock

            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            worker.process_event(event)

    # insert_validation_result should be called with a flag for duplicate lengths.
    # Filter for the duplicate-text deterministic call.
    dup_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and "duplicate ruling_text lengths" in (c.kwargs["result"].reason or "")
    ]
    assert len(dup_calls) >= 1, (
        f"Expected a duplicate ruling text flag, got calls: "
        f"{[c.kwargs.get('result') for c in mock_insert_validation.call_args_list]}"
    )
    dup_result = dup_calls[0].kwargs["result"]
    assert dup_result.result == "flag"
    assert f"length {len(same_length_text)} appears 3 times" in dup_result.reason


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_pass_distinct_ruling_text_lengths(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Split rulings with distinct text lengths should not flag (#2350)."""
    from ingestion.ruling_guards import ConvertedRuling

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        ("court-uuid-1",),
        ("case-uuid-2",),
        (True,),
    ]
    mock_cur.rowcount = 1

    # Create 2 ConvertedRuling objects with different ruling_text lengths.
    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text="Short ruling text.",
            case_number="23STCV12340",
            case_title="Smith v. Jones",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type="Motion for Summary Judgment",
            outcome="granted",
            hearing_date="2026-03-05",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            ruling_text="A much longer ruling text that discusses the merits in great detail.",
            case_number="23STCV12341",
            case_title="Doe v. Roe",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type="Demurrer",
            outcome="denied",
            hearing_date="2026-03-05",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            worker.process_event(event)

    # No duplicate ruling text flag should have been logged.
    dup_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and "duplicate ruling_text lengths" in (c.kwargs["result"].reason or "")
    ]
    assert len(dup_calls) == 0, (
        f"Unexpected duplicate ruling text flag: "
        f"{[c.kwargs.get('result') for c in mock_insert_validation.call_args_list]}"
    )


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_duplicate_flag_db_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If DB logging fails for a duplicate ruling text flag, the worker continues (#2350)."""
    from ingestion.ruling_guards import ConvertedRuling

    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        ("court-uuid-1",),
        ("case-uuid-2",),
        (True,),
        ("court-uuid-1",),
        ("case-uuid-3",),
        (True,),
    ]
    mock_cur.rowcount = 1

    same_length_text = "Identical ruling text." * 5
    converted = [
        ConvertedRuling(
            document_id=f"split-uuid-{i}",
            original_document_id="parent-uuid-1",
            split_index=i,
            split_count=3,
            is_multi=True,
            ruling_text=same_length_text,
            case_number=f"23STCV1234{i}",
            case_title=f"Case{i} v. Other{i}",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type=None,
            outcome=None,
            hearing_date="2026-03-05",
        )
        for i in range(3)
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 3)
            worker._framework_extractor = extractor_mock

            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            # Should not raise despite DB error during flag logging
            worker.process_event(event)


# ---------------------------------------------------------------------------
# Tests: document-level deterministic validation — cross-case ruling text
# misattribution (#2371)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_cross_case_ruling_text(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """A split ruling whose text references a sibling case number should flag (#2371)."""
    from ingestion.ruling_guards import ConvertedRuling

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # Two splits x (court + case + document) = 6 fetchone calls on the
    # happy path for split event processing.
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (split 0)
        ("case-uuid-1",),  # upsert_case (split 0)
        (True,),  # insert_document (split 0)
        ("court-uuid-1",),  # upsert_court (split 1)
        ("case-uuid-2",),  # upsert_case (split 1)
        (True,),  # insert_document (split 1)
    ]
    mock_cur.rowcount = 1

    # Split 0: ruling text correctly mentions its own case number only.
    # Split 1: ruling text mentions split 0's case number (misattribution).
    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text="Case 23STCV10000: the motion is granted.",
            case_number="23STCV10000",
            case_title="Smith v. Jones",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type="Motion for Summary Judgment",
            outcome="granted",
            hearing_date="2026-03-05",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            # BUG: this text references the first case's number.
            ruling_text="See ruling in 23STCV10000 for prior order details.",
            case_number="23STCV20000",
            case_title="Doe v. Roe",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type="Demurrer",
            outcome="denied",
            hearing_date="2026-03-05",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            worker.process_event(event)

    # Cross-case flag should have been logged for split-uuid-1.
    cross_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and "cross-case ruling text misattribution" in (c.kwargs["result"].reason or "")
    ]
    assert len(cross_calls) == 1, (
        f"Expected exactly one cross-case flag, got: "
        f"{[c.kwargs.get('result') for c in mock_insert_validation.call_args_list]}"
    )
    cross_result = cross_calls[0].kwargs["result"]
    assert cross_result.result == "flag"
    assert "23STCV10000" in cross_result.reason
    # Flag logged against the contaminated ruling's document_id.
    assert cross_calls[0].kwargs.get("document_id") == "split-uuid-1"


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_pass_no_cross_case_references(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Clean split rulings with no cross-case references should not flag (#2371)."""
    from ingestion.ruling_guards import ConvertedRuling

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        ("court-uuid-1",),
        ("case-uuid-2",),
        (True,),
    ]
    mock_cur.rowcount = 1

    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text="Case 23STCV10000: the motion is granted.",
            case_number="23STCV10000",
            case_title="Smith v. Jones",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type="Motion for Summary Judgment",
            outcome="granted",
            hearing_date="2026-03-05",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            ruling_text="Case 23STCV20000: the demurrer is sustained.",
            case_number="23STCV20000",
            case_title="Doe v. Roe",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type="Demurrer",
            outcome="denied",
            hearing_date="2026-03-05",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            worker.process_event(event)

    # No cross-case flag should have been logged.
    cross_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and "cross-case ruling text misattribution" in (c.kwargs["result"].reason or "")
    ]
    assert len(cross_calls) == 0, (
        f"Unexpected cross-case flag: "
        f"{[c.kwargs.get('result') for c in mock_insert_validation.call_args_list]}"
    )


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_cross_case_db_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If DB logging fails for a cross-case flag, the worker continues (#2371)."""
    from ingestion.ruling_guards import ConvertedRuling

    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        ("court-uuid-1",),
        ("case-uuid-2",),
        (True,),
    ]
    mock_cur.rowcount = 1

    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text="Case 23STCV10000: motion granted.",
            case_number="23STCV10000",
            case_title="Smith v. Jones",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type=None,
            outcome=None,
            hearing_date="2026-03-05",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            ruling_text="See ruling in 23STCV10000 for prior order details.",
            case_number="23STCV20000",
            case_title="Doe v. Roe",
            judge_name="Smith, John A.",
            department="Dept. 1",
            motion_type=None,
            outcome=None,
            hearing_date="2026-03-05",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            # Should not raise despite DB error during flag logging.
            worker.process_event(event)


# ---------------------------------------------------------------------------
# Tests: document-level deterministic validation — ruling_text case number
# marker mismatch (#2402)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_case_number_marker_mismatch(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Fresno-style ruling whose text embeds a DIFFERENT 'Superior Court Case
    No.' marker than its own case_number should flag even when the referenced
    number is not on the sibling list (#2402)."""
    from ingestion.ruling_guards import ConvertedRuling

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (split 0)
        ("case-uuid-1",),  # upsert_case (split 0)
        (True,),  # insert_document (split 0)
        ("court-uuid-1",),  # upsert_court (split 1)
        ("case-uuid-2",),  # upsert_case (split 1)
        (True,),  # insert_document (split 1)
    ]
    mock_cur.rowcount = 1

    # Split 0 is clean.  Split 1 has case_number 22CECG00930 but its
    # ruling_text embeds "Superior Court Case No. 24CECG03313" — a case
    # number that is NOT on the sibling list (split 0 is 25CECG01111), which
    # makes the existing cross-case rule miss it.  The new marker-mismatch
    # rule catches it.
    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text="Lopez v. Fresno USD: the motion is granted.",
            case_number="25CECG01111",
            case_title="Lopez v. Fresno USD",
            judge_name=None,
            department="403",
            motion_type="Demurrer",
            outcome="granted",
            hearing_date="2026-03-10",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            ruling_text=(
                "Re: Tarango v. Jackson et al.\n"
                "Superior Court Case No. 24CECG03313\n"
                "Tentative Ruling: To deny the motion."
            ),
            case_number="22CECG00930",
            case_title="Candler, Jr. v. Callender",
            judge_name=None,
            department="403",
            motion_type="Demurrer",
            outcome="denied",
            hearing_date="2026-03-10",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            # Use the default LA county in the event so the worker's
            # ``_llm_split_document`` uses the framework extractor (which we
            # pre-populate above) instead of a county-specific Google extractor
            # that needs a live API key.  The marker rule itself is
            # county-agnostic — it operates only on ruling_text and
            # case_number — so the county used to reach the code path does
            # not affect what is being asserted.
            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            worker.process_event(event)

    # Marker-mismatch flag should be logged exactly once, against the
    # misattributed ruling's document_id.  Guard against duplicate hits
    # from the cross-case rule by matching on the new reason substring.
    marker_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and "Superior Court Case No." in (c.kwargs["result"].reason or "")
    ]
    assert len(marker_calls) == 1, (
        f"Expected exactly one marker-mismatch flag, got: "
        f"{[c.kwargs.get('result') for c in mock_insert_validation.call_args_list]}"
    )
    marker_result = marker_calls[0].kwargs["result"]
    assert marker_result.result == "flag"
    assert "24CECG03313" in marker_result.reason
    assert "22CECG00930" in marker_result.reason
    # Flag logged against the contaminated ruling's document_id.
    assert marker_calls[0].kwargs.get("document_id") == "split-uuid-1"


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_pass_case_number_marker_matches(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Ruling whose 'Superior Court Case No.' marker matches its own case
    number should NOT flag (#2402)."""
    from ingestion.ruling_guards import ConvertedRuling

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        ("court-uuid-1",),
        ("case-uuid-2",),
        (True,),
    ]
    mock_cur.rowcount = 1

    # Both rulings are clean: marker matches own case_number.
    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text=(
                "Re: Lopez v. Fresno USD\n"
                "Superior Court Case No. 25CECG03271\n"
                "Tentative Ruling: To sustain."
            ),
            case_number="25CECG03271",
            case_title="Lopez v. Fresno USD",
            judge_name=None,
            department="403",
            motion_type="Demurrer",
            outcome="granted",
            hearing_date="2026-03-10",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            ruling_text=(
                "Re: Johnson v. World of Jeans\n"
                "Superior Court Case No. 23CECG00266\n"
                "Tentative Ruling: To deny."
            ),
            case_number="23CECG00266",
            case_title="Johnson v. World of Jeans",
            judge_name=None,
            department="403",
            motion_type="Motion",
            outcome="denied",
            hearing_date="2026-03-10",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            # Use default LA county so the framework extractor path runs.
            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            worker.process_event(event)

    # No marker-mismatch flag should have been logged.
    marker_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and "Superior Court Case No." in (c.kwargs["result"].reason or "")
    ]
    assert len(marker_calls) == 0, (
        f"Unexpected marker-mismatch flag: "
        f"{[c.kwargs.get('result') for c in mock_insert_validation.call_args_list]}"
    )


@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_case_number_marker_db_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If DB logging fails for a marker-mismatch flag, the worker continues (#2402)."""
    from ingestion.ruling_guards import ConvertedRuling

    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        ("court-uuid-1",),
        ("case-uuid-2",),
        (True,),
    ]
    mock_cur.rowcount = 1

    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text="Lopez v. Fresno USD: motion granted.",
            case_number="25CECG01111",
            case_title="Lopez v. Fresno USD",
            judge_name=None,
            department="403",
            motion_type=None,
            outcome=None,
            hearing_date="2026-03-10",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            ruling_text=("Superior Court Case No. 24CECG03313\nTentative Ruling: To deny."),
            case_number="22CECG00930",
            case_title="Candler, Jr. v. Callender",
            judge_name=None,
            department="403",
            motion_type=None,
            outcome=None,
            hearing_date="2026-03-10",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            # Use default LA county so the framework extractor path runs.
            event = _make_event(
                ruling_text="Full document text here " * 20,
            )
            # Should not raise despite DB error during flag logging.
            worker.process_event(event)


# ---------------------------------------------------------------------------
# Tests: empty-text rejection on split events (#2646)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_document_and_ruling")
@patch("ingestion.worker.insert_validation_result")
@patch(_DELETE_STALE_MOCK, return_value=0)
@patch("ingestion.worker.psycopg")
def test_det_validation_multi_case_empty_text_split_rejected(
    mock_psycopg: MagicMock,
    mock_delete_stale: MagicMock,
    mock_insert_validation: MagicMock,
    mock_insert_doc_ruling: MagicMock,
) -> None:
    """#2646 regression — multi-case split where one case has text and one
    does not.  The case with text is written; the empty-text case is
    rejected with a ``data_quality.ruling_empty_text_dropped`` telemetry
    marker on the validation-result reason field.

    Reproduces the Santa Clara failure mode: the multi-case-PDF LLM
    extraction produces N rulings, but extraction yields no text for one
    of them.  Before this fix the empty-text row silently reached
    ``derived.rulings`` with NULL ``ruling_text``; after the fix the
    worker's deterministic-validation path rejects it.  The test uses
    the default LA event (no county-specific extractor config) so the
    parent's ``_llm_split_document`` call honours the injected
    ``_framework_extractor`` mock — the bug is county-agnostic so this
    is a valid surrogate for the SC production path.
    """
    from ingestion.ruling_guards import ConvertedRuling

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # Only the first (non-empty) split needs DB fetches — the second is
    # rejected before insert_document_and_ruling runs.  upsert_court +
    # upsert_case are called before deterministic validation for split 0.
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (split 0)
        ("case-uuid-1",),  # upsert_case (split 0)
        (True,),  # insert_document (split 0)
        ("court-uuid-1",),  # upsert_court (split 1 — runs before det-validate)
        ("case-uuid-2",),  # upsert_case (split 1 — runs before det-validate)
    ]
    mock_cur.rowcount = 1
    # insert_document_and_ruling patched — simulate "new row" for split 0.
    mock_insert_doc_ruling.return_value = True

    # Split 0 has real text.  Split 1 has empty text — simulates the LLM
    # extracting metadata but failing to transcribe the body for the
    # second case in the PDF.
    converted = [
        ConvertedRuling(
            document_id="split-uuid-0",
            original_document_id="parent-uuid-1",
            split_index=0,
            split_count=2,
            is_multi=True,
            ruling_text="The motion for summary judgment is GRANTED.",
            case_number="23CV1111",
            case_title="Alpha v. Beta",
            judge_name="Smith, John A.",
            department="6",
            motion_type="motion_for_summary_judgment",
            outcome="granted",
            hearing_date="2026-03-05",
        ),
        ConvertedRuling(
            document_id="split-uuid-1",
            original_document_id="parent-uuid-1",
            split_index=1,
            split_count=2,
            is_multi=True,
            ruling_text=None,  # empty — the extraction failure under test
            case_number="23CV2222",
            case_title="Gamma v. Delta",
            judge_name="Smith, John A.",
            department="6",
            motion_type=None,
            outcome=None,
            hearing_date="2026-03-05",
        ),
    ]

    with patch(_CONVERT_MOCK, return_value=converted):
        with patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"):
            # Use the default LA event so _llm_split_document takes the
            # _get_framework_extractor() path (LA has no custom provider
            # config), which honours the extractor_mock assigned below.
            # County-specific configs (SC/Ventura/Fresno) route through
            # _get_county_extractor() which is harder to mock and would
            # return None without an API key — causing the parent to fall
            # through to single-document processing and write the parent
            # event's document_id instead of the split IDs.
            extractor_mock = MagicMock()
            extractor_mock.extract.return_value = MagicMock(rulings=[MagicMock()] * 2)
            worker._framework_extractor = extractor_mock

            event = _make_event(
                ruling_text="Full parent-PDF text here " * 20,
            )
            worker.process_event(event)

    # Exactly one document+ruling should have been written (the non-empty
    # split).  The empty-text split is rejected before insert runs.
    assert mock_insert_doc_ruling.call_count == 1, (
        f"Expected exactly one DB write, got {mock_insert_doc_ruling.call_count}. "
        f"The empty-text split should have been rejected by deterministic validation."
    )
    written_kwargs = mock_insert_doc_ruling.call_args_list[0].kwargs
    assert written_kwargs["document_id"] == "split-uuid-0"
    assert written_kwargs["ruling_text"] == "The motion for summary judgment is GRANTED."

    # The empty-text split must produce a deterministic-validation fail
    # record citing ruling_text_not_empty and the telemetry marker.
    fail_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result")
        and c.kwargs["result"].model == "deterministic"
        and c.kwargs["result"].result == "fail"
        and "ruling_text is null or empty" in (c.kwargs["result"].reason or "")
    ]
    assert len(fail_calls) == 1, (
        f"Expected one empty-text fail record, got: "
        f"{[c.kwargs.get('result') for c in mock_insert_validation.call_args_list]}"
    )
    fail_result = fail_calls[0].kwargs["result"]
    assert "data_quality.ruling_empty_text_dropped" in (fail_result.reason or "")
    # The fail record must be attached to the empty split's document_id,
    # not the parent's — this is the row that would have gone to DB.
    assert fail_calls[0].kwargs["document_id"] == "split-uuid-1"
