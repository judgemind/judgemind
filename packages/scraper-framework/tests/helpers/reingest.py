"""Shared test helpers for parse_document reingest-path regression tests.

Several scrapers (CourtListener #3986, Contra Costa portal #4133,
SF civil #4134) have parse_document implementations that on the live
capture path receive a ``CapturedDocument`` whose structured fields
(``case_number``, ``judge_name``, ``ruling_text``, ...) have already been
populated upstream — and therefore historically were free to be no-ops or
to assume those fields were set.  Reingest from S3
(``scripts/reingest_from_s3.py::_reparse_document``) breaks that
assumption: it constructs a fresh ``CapturedDocument`` with only
``raw_content`` and the identifier fields set, then calls
``parse_document(cap_doc)``.  Any field that ``parse_document`` does not
explicitly populate gets clobbered to the default by the merge logic
downstream.

The fix in each scraper is to make ``parse_document`` populate every
relevant field from ``raw_content`` alone.  The regression test for that
fix needs to construct exactly the reingest-shape ``CapturedDocument``
and assert structured fields are recovered after parse_document runs.

This module provides the shared scaffold so each scraper's regression
test does not re-derive the cap_doc construction inline.

See:
- ``scripts/reingest_from_s3.py::_reparse_document`` (lines ~919-938)
- Issue #4046 audit: ``docs/investigations/parse_document-reingest-safety-2026-05.md``
- Issue #4153 (this helper)
"""

from __future__ import annotations

from datetime import UTC, datetime

from framework import CapturedDocument, ContentFormat


def make_reingest_cap_doc(
    *,
    raw_content: bytes,
    scraper_id: str,
    state: str = "CA",
    county: str = "Test",
    court: str = "Test Superior Court",
    source_url: str = "https://example.com/doc",
    content_format: ContentFormat = ContentFormat.TEXT,
    document_id: str = "test-doc-id",
    capture_timestamp: datetime | None = None,
    content_hash: str = "deadbeef" * 8,
) -> CapturedDocument:
    """Build a CapturedDocument matching ``scripts/reingest_from_s3.py`` shape.

    Mirrors the production reingest shape: only ``raw_content`` and the
    identifier fields are set; every parsed field
    (``case_number``, ``case_title``, ``judge_name``, ``hearing_date``,
    ``ruling_text``, ``ruling_text_html``, ``outcome``, ``motion_type``,
    ``parties``, ``extra``, ``courthouse``, ``department``) is left at its
    default (``None`` / ``[]`` / ``{}``).  The structured-field
    population MUST come from ``parse_document`` reading ``raw_content``
    — exactly the contract the regression-test suites enforce.

    Parameters
    ----------
    raw_content
        The bytes ``parse_document`` will read.  Test cases pass either
        a JSON envelope (the live-capture archive shape), arbitrary
        non-JSON bytes (the "decode-fails-gracefully" tolerance case),
        or empty bytes (the "defensive-empty-input" case).
    scraper_id
        The scraper's canonical id (e.g. ``federal-courtlistener-opinions``,
        ``ca-cc-tentatives-portal``, ``ca-sf-civil-tentatives``).  Required
        — every scraper has its own and there is no defensible default.
    state, county, court, source_url, content_format, document_id, content_hash
        Identifier-shape fields.  Defaults match the reingest production
        path's choices but each is overridable per-call.
    capture_timestamp
        Defaults to a fixed deterministic ``2026-01-01T00:00:00Z`` if not
        provided — keeps regression-test goldens stable across reruns.
    """
    if capture_timestamp is None:
        capture_timestamp = datetime(2026, 1, 1, tzinfo=UTC)

    return CapturedDocument(
        document_id=document_id,
        scraper_id=scraper_id,
        state=state,
        county=county,
        court=court,
        source_url=source_url,
        capture_timestamp=capture_timestamp,
        content_format=content_format,
        raw_content=raw_content,
        content_hash=content_hash,
    )


# Field names populated by parse_document on the reingest path.  The
# defensive "raw_content fails to decode → return unchanged" tolerance
# tests assert that every one of these stays at its model-default after
# parse_document runs against unparseable raw_content.
_PARSE_POPULATED_SCALAR_FIELDS = (
    "case_number",
    "case_title",
    "courthouse",
    "department",
    "judge_name",
    "hearing_date",
    "ruling_text",
    "ruling_text_html",
    "outcome",
    "motion_type",
)


def assert_structured_fields_unchanged(doc: CapturedDocument) -> None:
    """Assert every parse_document-populated field is at its default.

    Useful for the "raw_content fails to decode → return unchanged"
    tolerance regression tests: pre-parse_document the cap_doc carries
    only raw_content + identifiers, so these defaults must hold; if
    parse_document tolerates the bad input correctly the same defaults
    must still hold post-parse_document.

    Tests that want to assert pre-parse defaults can call this on the
    fresh cap_doc; tests that want to assert post-parse-of-bad-input
    can call it on the parsed result.

    Raises ``AssertionError`` if any populated-on-success field has
    drifted from its model default.
    """
    for field_name in _PARSE_POPULATED_SCALAR_FIELDS:
        actual = getattr(doc, field_name)
        assert actual is None, f"expected {field_name} to be None (default), got {actual!r}"
    assert doc.parties == [], f"expected parties to be [] (default), got {doc.parties!r}"
    assert doc.extra == {}, f"expected extra to be {{}} (default), got {doc.extra!r}"
