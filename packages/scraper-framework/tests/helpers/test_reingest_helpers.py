"""Unit tests for ``tests/helpers/reingest.py``.

These tests pin the contract that the reingest-cap_doc helper:

1. Returns a ``CapturedDocument`` with ONLY ``raw_content`` and
   identifier fields set (every parsed field at default).
2. Lets per-call kwargs override the identifier defaults.
3. ``assert_structured_fields_unchanged`` accepts the helper's output
   unchanged and rejects any drift on a populated parsed field.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from framework import CapturedDocument, ContentFormat
from helpers.reingest import (
    assert_structured_fields_unchanged,
    make_reingest_cap_doc,
)


class TestMakeReingestCapDoc:
    def test_required_fields_only_returns_cap_doc_with_defaults(self) -> None:
        """With only ``raw_content`` and ``scraper_id`` set, every parsed
        field is at its model-default and identifier fields take the
        helper's documented defaults.
        """
        doc = make_reingest_cap_doc(
            raw_content=b"hello",
            scraper_id="federal-courtlistener-opinions",
        )

        assert isinstance(doc, CapturedDocument)
        # raw_content + identifiers populated.
        assert doc.raw_content == b"hello"
        assert doc.scraper_id == "federal-courtlistener-opinions"
        assert doc.state == "CA"
        assert doc.county == "Test"
        assert doc.court == "Test Superior Court"
        assert doc.source_url == "https://example.com/doc"
        assert doc.content_format == ContentFormat.TEXT
        assert doc.document_id == "test-doc-id"
        assert doc.content_hash == "deadbeef" * 8

        # capture_timestamp defaults to a deterministic value.
        assert doc.capture_timestamp == datetime(2026, 1, 1, tzinfo=UTC)

        # Every parse_document-populated field is at its default — this
        # is the core invariant the reingest helper exists to enforce.
        assert doc.case_number is None
        assert doc.case_title is None
        assert doc.courthouse is None
        assert doc.department is None
        assert doc.judge_name is None
        assert doc.hearing_date is None
        assert doc.ruling_text is None
        assert doc.ruling_text_html is None
        assert doc.outcome is None
        assert doc.motion_type is None
        assert doc.parties == []
        assert doc.extra == {}

    def test_overrides_propagate(self) -> None:
        """Per-call kwargs override every identifier-shape default."""
        ts = datetime(2025, 6, 30, 12, 0, 0, tzinfo=UTC)
        doc = make_reingest_cap_doc(
            raw_content=b"{}",
            scraper_id="ca-cc-tentatives-portal",
            state="CA",
            county="Contra Costa",
            court="Superior Court",
            source_url="https://contracosta.courts.ca.gov/tentative-ruling/x",
            content_format=ContentFormat.TEXT,
            document_id="custom-doc-id",
            capture_timestamp=ts,
            content_hash="cafef00d" * 8,
        )

        assert doc.scraper_id == "ca-cc-tentatives-portal"
        assert doc.county == "Contra Costa"
        assert doc.court == "Superior Court"
        assert doc.source_url == "https://contracosta.courts.ca.gov/tentative-ruling/x"
        assert doc.document_id == "custom-doc-id"
        assert doc.capture_timestamp == ts
        assert doc.content_hash == "cafef00d" * 8

    def test_empty_raw_content_is_allowed(self) -> None:
        """The defensive empty-bytes case must not raise — parse_document
        tolerance tests rely on it.
        """
        doc = make_reingest_cap_doc(
            raw_content=b"",
            scraper_id="federal-courtlistener-opinions",
        )
        assert doc.raw_content == b""


class TestAssertStructuredFieldsUnchanged:
    def test_accepts_fresh_cap_doc(self) -> None:
        """A fresh helper-built cap_doc must satisfy the assertion."""
        doc = make_reingest_cap_doc(
            raw_content=b"hello",
            scraper_id="federal-courtlistener-opinions",
        )
        # No exception expected.
        assert_structured_fields_unchanged(doc)

    @pytest.mark.parametrize(
        "field_name,value",
        [
            ("case_number", "22-1234"),
            ("case_title", "Smith v. Jones"),
            ("judge_name", "Justice Roberts"),
            ("hearing_date", datetime(2026, 3, 1, tzinfo=UTC)),
            ("ruling_text", "MOTION GRANTED"),
            ("ruling_text_html", "<p>MOTION GRANTED</p>"),
            ("outcome", "GRANTED"),
            ("motion_type", "Demurrer"),
            ("courthouse", "Martinez Courthouse"),
            ("department", "16"),
        ],
    )
    def test_rejects_populated_scalar(self, field_name: str, value: object) -> None:
        """Drift on any populated-on-success scalar field must raise."""
        doc = make_reingest_cap_doc(
            raw_content=b"hello",
            scraper_id="federal-courtlistener-opinions",
        )
        # Mutate the field via model_copy so we don't fight Pydantic.
        mutated = doc.model_copy(update={field_name: value})

        with pytest.raises(AssertionError, match=field_name):
            assert_structured_fields_unchanged(mutated)

    def test_rejects_populated_parties(self) -> None:
        """Drift on ``parties`` (list field) must raise."""
        doc = make_reingest_cap_doc(
            raw_content=b"hello",
            scraper_id="federal-courtlistener-opinions",
        )
        mutated = doc.model_copy(update={"parties": [{"role": "plaintiff", "name": "Smith"}]})
        with pytest.raises(AssertionError, match="parties"):
            assert_structured_fields_unchanged(mutated)

    def test_rejects_populated_extra(self) -> None:
        """Drift on ``extra`` (dict field) must raise."""
        doc = make_reingest_cap_doc(
            raw_content=b"hello",
            scraper_id="federal-courtlistener-opinions",
        )
        mutated = doc.model_copy(update={"extra": {"slug": "x"}})
        with pytest.raises(AssertionError, match="extra"):
            assert_structured_fields_unchanged(mutated)
