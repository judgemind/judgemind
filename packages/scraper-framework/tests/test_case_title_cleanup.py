"""Tests for the case_title cleanup helper used by IngestionWorker (#3615).

Covers ``apply_case_title_cleanup()`` — the helper extracted from worker.py
that applies deterministic cleanup to implausible LLM-extracted case
titles, with a hard NULL fallback when cleanup cannot produce a plausible
title.

Before #3615, the cleanup branch in worker.py silently kept the original
contaminated title when ``clean_case_title()`` returned None or another
implausible value.  This file's tests pin the new better-null-than-wrong
behavior so the regression cannot recur.
"""

from __future__ import annotations

import logging

import pytest

from ingestion.worker import apply_case_title_cleanup


class TestApplyCaseTitleCleanupRecoverable:
    """Cases where deterministic cleanup successfully recovers a clean title."""

    def test_returns_cleaned_title_for_recoverable_input(self) -> None:
        """A title cleanable to a plausible result is returned with the
        ``deterministic_cleaned`` extraction-method tag."""
        # The procedural keyword 'REQUEST FOR' is in _TITLE_TERMINATOR_RE so
        # the cleaner truncates the contamination and returns clean party names.
        raw = "Cuong Quach vs Maxreal Cupertino REQUEST FOR FORM INTERROGATORY NO. 12.1"
        cleaned, method = apply_case_title_cleanup(raw, document_id="doc-1")
        assert cleaned is not None
        assert method == "deterministic_cleaned"
        assert "Quach" in cleaned
        assert "REQUEST FOR" not in cleaned
        assert "INTERROGATORY" not in cleaned

    def test_returns_cleaned_title_for_cause_of_action_contamination(self) -> None:
        """#3615: 'Sixth Cause of Action' is a new terminator pattern."""
        raw = "STEINMAN VS. FORD MOTOR COMPANY Sixth Cause of Action - Fraudulent Inducement"
        cleaned, method = apply_case_title_cleanup(raw, document_id="doc-2")
        # Either cleanup produces a plausible result (preferred) or NULL
        # fallback fires.  Both are acceptable for the helper contract.
        if method == "deterministic_cleaned":
            assert cleaned is not None
            assert "Steinman" in cleaned or "STEINMAN" in cleaned
            assert "Cause of Action" not in cleaned
        else:
            assert cleaned is None
            assert method is None


class TestApplyCaseTitleCleanupUnrecoverable:
    """Cases where deterministic cleanup cannot produce a plausible title.
    The helper must return (None, None) and emit case_title.unrecoverable.
    This is the #3615 better-null-than-wrong fix.
    """

    def test_returns_none_when_clean_returns_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """When ``clean_case_title`` returns None (e.g. no v. separator), the
        helper returns (None, None) and emits the unrecoverable log.
        """
        # No v./vs. separator — clean_case_title returns None.
        raw = "MOTION TO COMPEL DISCOVERY"
        with caplog.at_level(logging.WARNING):
            cleaned, method = apply_case_title_cleanup(raw, document_id="doc-3")
        assert cleaned is None
        assert method is None
        # Verify the unrecoverable warning was emitted.
        unrecoverable_records = [
            r for r in caplog.records if r.message == "case_title.unrecoverable"
        ]
        assert len(unrecoverable_records) == 1
        # Spot-check the structured fields.
        rec = unrecoverable_records[0]
        assert getattr(rec, "document_id", None) == "doc-3"
        assert getattr(rec, "telemetry_event", None) == "case_title_unrecoverable"
        # raw_title should be truncated to 200 chars.
        raw_title_attr = getattr(rec, "raw_title", "")
        assert raw_title_attr.startswith("MOTION TO COMPEL")

    def test_returns_none_when_clean_returns_implausible(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When ``clean_case_title`` returns a value that ALSO fails
        ``is_plausible_case_title``, the helper returns (None, None) — this
        is the silent-failure-fallback bug from #3615.
        """
        # Construct an input where the cleaner produces something that
        # fails plausibility.  The LLM concatenates the body sentence
        # "Plaintiff X's ..." after the caption — cleaner can't truncate
        # cleanly, so the result either fails plausibility or matches the
        # raw input.
        # Use a raw title that starts with an implausible prefix per the
        # plausibility regex, AND for which clean_case_title cannot
        # produce a clean v. separator.
        raw = (
            "To Respond Without Objections Cause of Action"  # implausible prefix + procedural body
        )
        with caplog.at_level(logging.WARNING):
            cleaned, method = apply_case_title_cleanup(raw, document_id="doc-4")
        assert cleaned is None
        assert method is None
        unrecoverable_records = [
            r for r in caplog.records if r.message == "case_title.unrecoverable"
        ]
        assert len(unrecoverable_records) == 1

    def test_emits_unrecoverable_log_with_truncated_raw_title(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The raw_title field in the log is truncated to 200 chars to
        avoid log bloat.
        """
        raw = "MOTION " * 50  # 350 chars; no v. separator.
        with caplog.at_level(logging.WARNING):
            apply_case_title_cleanup(raw, document_id="doc-long")
        unrecoverable_records = [
            r for r in caplog.records if r.message == "case_title.unrecoverable"
        ]
        assert len(unrecoverable_records) == 1
        raw_title_attr = getattr(unrecoverable_records[0], "raw_title", "")
        assert len(raw_title_attr) <= 200

    def test_does_not_emit_log_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful cleanup does NOT emit the unrecoverable warning."""
        raw = "Cuong Quach vs Maxreal Cupertino REQUEST FOR FORM INTERROGATORY"
        with caplog.at_level(logging.WARNING):
            cleaned, method = apply_case_title_cleanup(raw, document_id="doc-ok")
        if method == "deterministic_cleaned":
            unrecoverable_records = [
                r for r in caplog.records if r.message == "case_title.unrecoverable"
            ]
            assert len(unrecoverable_records) == 0


class TestApplyCaseTitleCleanupSignature:
    """Public contract / signature tests."""

    def test_document_id_optional(self) -> None:
        """document_id is keyword-only and optional."""
        cleaned, method = apply_case_title_cleanup("Smith vs Jones REQUEST FOR")
        # The result depends on the cleaner; the contract is just that
        # the call doesn't raise without document_id.
        assert isinstance(cleaned, (str, type(None)))
        assert isinstance(method, (str, type(None)))

    def test_returns_two_tuple(self) -> None:
        """Return shape is always a 2-tuple of (Optional[str], Optional[str])."""
        result = apply_case_title_cleanup("MOTION TO COMPEL", document_id="doc-x")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_when_method_is_none_title_is_none(self) -> None:
        """Invariant: if method is None, title must also be None
        (the caller in worker.py relies on this when popping
        extraction_methods["case_title"]).
        """
        # Force unrecoverable.
        cleaned, method = apply_case_title_cleanup(
            "MOTION TO COMPEL DISCOVERY", document_id="doc-x"
        )
        if method is None:
            assert cleaned is None
        else:
            assert method == "deterministic_cleaned"
            assert cleaned is not None


class TestEmptyAndWhitespaceCaseTitle:
    """#3615 spotcheck (2026-04-28) — case_title='' is a third state.

    The empty-string normalization happens in worker.py at the top of
    the cleanup block, BEFORE apply_case_title_cleanup is called.  The
    helper itself is documented to receive a non-empty input (caller
    contract), but we test the prior caller-side normalization here for
    completeness.

    The actual normalization assertion happens at the worker integration
    layer.  This test class documents the expected behavior.
    """

    def test_empty_string_short_circuits_worker_cleanup_block(self) -> None:
        """The cleanup block in worker.py guards with ``if case_title:`` so
        an empty string is never passed to apply_case_title_cleanup.
        Combined with the #3615 normalization that explicitly sets
        case_title=None when it's whitespace-only, the downstream code
        sees a single 'missing' sentinel.

        This is a documentation test — it asserts that when the worker
        normalization fires, both case_title and the extraction_methods
        entry are cleared.  The actual normalization logic is at
        ``_process_message`` line ~1973 (commit boundary in worker.py).
        """
        # Simulate what the worker normalization block does:
        case_title: str | None = ""
        extraction_methods: dict[str, str] = {"case_title": "llm"}

        # The worker.py normalization:
        if case_title is not None and not case_title.strip():
            case_title = None
            extraction_methods.pop("case_title", None)

        assert case_title is None
        assert "case_title" not in extraction_methods

    def test_whitespace_only_short_circuits_worker_cleanup_block(self) -> None:
        """Whitespace-only string also normalizes to None."""
        case_title: str | None = "   \t\n  "
        extraction_methods: dict[str, str] = {"case_title": "llm"}

        if case_title is not None and not case_title.strip():
            case_title = None
            extraction_methods.pop("case_title", None)

        assert case_title is None
        assert "case_title" not in extraction_methods

    def test_non_empty_does_not_short_circuit(self) -> None:
        """A non-empty title flows through unchanged."""
        case_title: str | None = "Smith v. Jones"
        extraction_methods: dict[str, str] = {"case_title": "llm"}

        if case_title is not None and not case_title.strip():
            case_title = None
            extraction_methods.pop("case_title", None)

        assert case_title == "Smith v. Jones"
        assert extraction_methods == {"case_title": "llm"}


# ---------------------------------------------------------------------------
# Worker integration: exercise the cleanup block via IngestionWorker.process_event
# ---------------------------------------------------------------------------
#
# The unit tests above cover the helper in isolation. The integration tests
# below drive the worker.process_event() entrypoint to confirm the cleanup
# block (worker.py:2041-2043 empty-string normalization, worker.py:2134-2140
# helper invocation + extraction_methods update) is wired up correctly.
#
# Pattern matches existing test_ingestion.py — mock psycopg + OpenSearch +
# Redis, supply a synthesized event, observe the persisted state.

from unittest.mock import MagicMock, patch  # noqa: E402

from ingestion.worker import IngestionWorker  # noqa: E402


def _make_worker_for_integration() -> tuple[IngestionWorker, MagicMock]:
    """Return a worker with mocked external dependencies for cleanup-block tests."""
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False
    worker = IngestionWorker(
        redis_client=redis_mock,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=os_mock,
        s3_client=s3_mock,
        archive_bucket="test-bucket",
    )
    worker._get_framework_extractor = lambda: None  # type: ignore[method-assign]
    worker._enrichment_client = None
    return worker, os_mock


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (mock_conn, mock_cur) pair configured for the persistent connection pattern."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def _make_event(**overrides: object) -> dict:
    """Return a minimal event payload that flows through the cleanup block."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000999",
        "scraper_id": "ca-la-tentatives-civil",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": "https://www.lacourt.org/tentativerulings/test",
        "content_format": "html",
        "content_hash": "deadbeef",
        "s3_key": "ca/los_angeles/superior_court/raw/deadbeef.html",
        "s3_bucket": "judgemind-document-archive-dev",
        "case_number": "23STCV99999",
        "department": "Dept. 1",
        "judge_name": "Smith, John A.",
        "ruling_text": "The motion is GRANTED.",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


class TestWorkerEmptyStringNormalization:
    """#3615 spotcheck (2026-04-28) — empty-string case_title normalization
    is exercised end-to-end through ``IngestionWorker.process_event``.

    These tests assert the *strong* invariant that ``upsert_case_returning_title``
    receives ``case_title=None`` (not the empty string), addressing the
    adversarial reviewer's concern that asserting only the SQL string cannot
    distinguish ``''`` from ``NULL``.
    """

    @patch("ingestion.worker.upsert_case_returning_title")
    @patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_empty_string_case_title_normalized_to_none(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_court: MagicMock,
        mock_upsert_case: MagicMock,
    ) -> None:
        """An event with case_title="" results in upsert_case_returning_title
        being called with case_title=None (NOT empty string)."""
        worker, _os_mock = _make_worker_for_integration()
        mock_conn, _mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        # upsert_case_returning_title returns (case_id, effective_title).
        mock_upsert_case.return_value = ("case-uuid-1", None)
        # insert_document_and_ruling is the next SQL-touching call; let it
        # succeed by configuring the cursor's fetchone for that path.
        # Because we've mocked upsert_court and upsert_case_returning_title,
        # the cursor only sees insert_document_and_ruling-related calls.
        mock_cur = mock_conn.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = (True,)  # insert_document RETURNING is_new
        mock_cur.rowcount = 1

        event = _make_event(case_title="")  # explicit empty string
        worker.process_event(event)

        # The strong assertion: upsert_case_returning_title was called with
        # case_title=None (not "").
        mock_upsert_case.assert_called_once()
        call_kwargs = mock_upsert_case.call_args.kwargs
        # case_title may be passed positionally or as kwarg — handle both.
        if "case_title" in call_kwargs:
            assert call_kwargs["case_title"] is None
        else:
            # Positional: signature is
            # (conn, case_number, court_id, case_title, case_type, *, force_update)
            args = mock_upsert_case.call_args.args
            assert len(args) >= 4
            assert args[3] is None  # case_title position

    @patch("ingestion.worker.upsert_case_returning_title")
    @patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_whitespace_only_case_title_normalized_to_none(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_court: MagicMock,
        mock_upsert_case: MagicMock,
    ) -> None:
        """An event with case_title="   \\t\\n  " also normalizes to None."""
        worker, _os_mock = _make_worker_for_integration()
        mock_conn, _mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_upsert_case.return_value = ("case-uuid-1", None)
        mock_cur = mock_conn.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = (True,)
        mock_cur.rowcount = 1

        event = _make_event(case_title="   \t\n  ")
        worker.process_event(event)

        mock_upsert_case.assert_called_once()
        call_kwargs = mock_upsert_case.call_args.kwargs
        if "case_title" in call_kwargs:
            assert call_kwargs["case_title"] is None
        else:
            args = mock_upsert_case.call_args.args
            assert len(args) >= 4
            assert args[3] is None

    @patch("ingestion.worker.upsert_case_returning_title")
    @patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_legitimate_title_passed_through_unchanged(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_court: MagicMock,
        mock_upsert_case: MagicMock,
    ) -> None:
        """A plausible non-empty title is passed through to upsert unchanged.

        This is the negative regression case for the empty-string normalization
        — confirming the new code does NOT null legitimate titles.
        """
        worker, _os_mock = _make_worker_for_integration()
        mock_conn, _mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_upsert_case.return_value = ("case-uuid-1", None)
        mock_cur = mock_conn.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = (True,)
        mock_cur.rowcount = 1

        event = _make_event(case_title="Smith v. Jones")
        worker.process_event(event)

        mock_upsert_case.assert_called_once()
        call_kwargs = mock_upsert_case.call_args.kwargs
        case_title_value = call_kwargs.get("case_title", None)
        if case_title_value is None:
            args = mock_upsert_case.call_args.args
            case_title_value = args[3] if len(args) >= 4 else None
        assert case_title_value == "Smith v. Jones"


class TestWorkerCleanupHelperWired:
    """#3615 — the worker invokes ``apply_case_title_cleanup`` for implausible
    titles and persists the result.

    Adversarial-review hardening: each test asserts the actual ``case_title``
    value passed to ``upsert_case_returning_title`` (the DB-persistence
    layer), not just a logging side-effect.  This pins the end-to-end state
    change so a future refactor that breaks the ``case_title = None``
    assignment but keeps the log call would still fail these tests.
    """

    @staticmethod
    def _extract_persisted_case_title(mock_upsert_case: MagicMock) -> str | None:
        """Return the case_title value passed to upsert_case_returning_title.

        Handles both kwarg and positional invocation forms.  The function's
        signature is
        ``(conn, case_number, court_id, case_title=None, case_type=None, *, force_update=False)``,
        so positional case_title is at index 3.
        """
        call_kwargs = mock_upsert_case.call_args.kwargs
        if "case_title" in call_kwargs:
            return call_kwargs["case_title"]
        args = mock_upsert_case.call_args.args
        if len(args) >= 4:
            return args[3]
        return None

    @patch("ingestion.worker.upsert_case_returning_title")
    @patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_unrecoverable_title_is_persisted_as_null(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_court: MagicMock,
        mock_upsert_case: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A title that fails plausibility AND can't be cleaned results in
        ``case_title=None`` being passed to ``upsert_case_returning_title``,
        AND a ``case_title.unrecoverable`` warning being emitted.

        This is the primary outcome of #3615 Change 3: better-null-than-wrong.
        """
        worker, _os_mock = _make_worker_for_integration()
        mock_conn, _mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_upsert_case.return_value = ("case-uuid-1", None)
        mock_cur = mock_conn.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = (True,)  # insert_document RETURNING is_new
        mock_cur.rowcount = 1

        # Procedural-prefix "MOTION" + no v. separator → clean_case_title
        # returns None → helper returns (None, None) → worker persists NULL.
        event = _make_event(case_title="MOTION TO COMPEL DISCOVERY OF EVIDENCE")

        with caplog.at_level(logging.WARNING):
            worker.process_event(event)

        # Assertion 1: the unrecoverable warning was emitted.
        unrecoverable_records = [
            r for r in caplog.records if r.message == "case_title.unrecoverable"
        ]
        assert len(unrecoverable_records) == 1

        # Assertion 2 (the load-bearing one): the persisted case_title is NULL.
        mock_upsert_case.assert_called_once()
        persisted = self._extract_persisted_case_title(mock_upsert_case)
        assert persisted is None

    @patch("ingestion.worker.upsert_case_returning_title")
    @patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_recoverable_implausible_title_persists_cleaned_value(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_court: MagicMock,
        mock_upsert_case: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A recoverable implausible title is cleaned and the cleaned value
        (NOT the original contamination) is persisted to the DB.

        Adversarial-review hardening: assert on the actual persisted value
        rather than absence of a log.
        """
        worker, _os_mock = _make_worker_for_integration()
        mock_conn, _mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_upsert_case.return_value = ("case-uuid-1", None)
        mock_cur = mock_conn.cursor.return_value.__enter__.return_value
        mock_cur.fetchone.return_value = (True,)
        mock_cur.rowcount = 1

        # Implausible-and-recoverable: 'Sixth Cause of Action' is in the
        # widened terminator regex so clean_case_title cuts the contamination
        # and returns clean party names.
        raw = "STEINMAN VS. FORD MOTOR COMPANY Sixth Cause of Action - Fraud"
        event = _make_event(case_title=raw)

        with caplog.at_level(logging.WARNING):
            worker.process_event(event)

        # The cleaner should have returned a plausible value, so:
        # (a) no unrecoverable log
        unrecoverable_records = [
            r for r in caplog.records if r.message == "case_title.unrecoverable"
        ]
        assert len(unrecoverable_records) == 0

        # (b) the persisted value is the cleaned title (NOT the original
        #     contamination, NOT None).
        mock_upsert_case.assert_called_once()
        persisted = self._extract_persisted_case_title(mock_upsert_case)
        assert persisted is not None
        # Cleaned value must contain the legitimate party names...
        assert "Steinman" in persisted or "STEINMAN" in persisted
        assert "Ford" in persisted or "FORD" in persisted
        # ...and must NOT contain the procedural contamination.
        assert "Cause of Action" not in persisted
        assert "Sixth" not in persisted
