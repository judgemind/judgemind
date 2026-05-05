"""Tests for framework.models — focused on enum helpers shared by the
ingestion pipeline and the reingest-from-S3 script.

Regression coverage for #4122: the DB ``derived.documents.format`` enum
stores ``'txt'``, but the Python ``ContentFormat`` enum spells it
``'text'``.  ``ContentFormat.from_db_value`` is the single helper that
bridges the two; the script-side fix at
``scripts/reingest_from_s3.py`` calls it at three sites that previously
raised ``ValueError`` for every Federal CourtListener ``.txt``
document.
"""

from __future__ import annotations

import pytest

from framework.models import ContentFormat


class TestContentFormatFromDbValue:
    """``ContentFormat.from_db_value`` maps DB enum values to enum members.

    The DB ``document_format`` enum (``packages/api/migrations/1_initial-schema.sql``
    line 166) is ``('html', 'pdf', 'docx', 'txt')``; the Python enum uses
    ``'text'`` for the textual format.  This helper is the only sanctioned
    place to bridge that gap (#4122).
    """

    def test_txt_maps_to_text(self) -> None:
        # The bug case — DB-side 'txt' must round-trip to TEXT without raising.
        # Pre-fix, ContentFormat('txt') raised ValueError silently swallowed by
        # reingest_from_s3.py's parse_document fallback path.
        assert ContentFormat.from_db_value("txt") is ContentFormat.TEXT

    def test_html_maps_to_html(self) -> None:
        assert ContentFormat.from_db_value("html") is ContentFormat.HTML

    def test_pdf_maps_to_pdf(self) -> None:
        assert ContentFormat.from_db_value("pdf") is ContentFormat.PDF

    def test_docx_maps_to_docx(self) -> None:
        assert ContentFormat.from_db_value("docx") is ContentFormat.DOCX

    def test_unknown_value_raises_value_error(self) -> None:
        # An unknown DB value must raise ValueError (not KeyError) so the
        # call sites in reingest_from_s3.py — and any future consumer —
        # see a uniform exception type aligned with the previous
        # ``ContentFormat(<bad>)`` constructor behavior.
        with pytest.raises(ValueError, match="not a valid"):
            ContentFormat.from_db_value("rtf")

    def test_text_is_not_a_valid_db_value(self) -> None:
        # Reverse direction sanity check — the Python-side spelling
        # 'text' is NOT a valid DB value.  The helper is one-way: it
        # accepts the DB enum's value set, not the Python enum's value
        # set.  This guards against an accidental "make it bidirectional"
        # refactor that would silently swallow the original bug class.
        with pytest.raises(ValueError, match="not a valid"):
            ContentFormat.from_db_value("text")
