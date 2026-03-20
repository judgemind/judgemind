"""Tests for the backfill_css_inlining script.

All database and S3 access is mocked — these tests verify the logic
without requiring live infrastructure.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import httpx

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill = importlib.import_module("backfill_css_inlining")


# ---------------------------------------------------------------------------
# _has_inline_styles
# ---------------------------------------------------------------------------


def test_has_inline_styles_detects_style_tag() -> None:
    assert backfill._has_inline_styles(b"<html><head><style>body{}</style></head></html>")


def test_has_inline_styles_case_insensitive() -> None:
    assert backfill._has_inline_styles(b"<html><head><STYLE>body{}</STYLE></head></html>")


def test_has_inline_styles_returns_false_for_no_style() -> None:
    assert not backfill._has_inline_styles(b"<html><head></head><body></body></html>")


def test_has_inline_styles_detects_style_with_attributes() -> None:
    html = b'<html><head><style type="text/css">body{}</style></head></html>'
    assert backfill._has_inline_styles(html)


# ---------------------------------------------------------------------------
# process_document
# ---------------------------------------------------------------------------


def test_process_document_skips_already_styled() -> None:
    """Documents that already have inline styles should be skipped."""
    html_with_style = b"<html><head><style>body{}</style></head><body></body></html>"

    mock_s3 = MagicMock()
    body_mock = MagicMock(read=MagicMock(return_value=html_with_style))
    mock_s3.get_object.return_value = {"Body": body_mock}

    mock_http = MagicMock(spec=httpx.Client)

    result = backfill.process_document(
        mock_s3,
        mock_http,
        "doc-1",
        "key.html",
        "bucket",
        "https://court.example.com/page.html",
    )

    assert result == "skipped_has_styles"
    mock_s3.put_object.assert_not_called()


def test_process_document_skips_no_change() -> None:
    """Documents where CSS inlining produces no change should be skipped."""
    html_no_css = b"<html><head></head><body><p>Hello</p></body></html>"

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=html_no_css))}

    mock_http = MagicMock(spec=httpx.Client)

    with patch("backfill_css_inlining.inline_css", return_value=html_no_css):
        result = backfill.process_document(
            mock_s3,
            mock_http,
            "doc-1",
            "key.html",
            "bucket",
            "https://court.example.com/page.html",
        )

    assert result == "skipped_no_change"
    mock_s3.put_object.assert_not_called()


def test_process_document_inlines_and_uploads() -> None:
    """Documents with external CSS should be inlined and re-uploaded."""
    original = b"<html><head><link rel='stylesheet' href='/css/s.css'></head><body></body></html>"
    inlined = b"<html><head><style>body{}</style></head><body></body></html>"

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=original))}

    mock_http = MagicMock(spec=httpx.Client)

    with patch("backfill_css_inlining.inline_css", return_value=inlined):
        result = backfill.process_document(
            mock_s3,
            mock_http,
            "doc-1",
            "key.html",
            "bucket",
            "https://court.example.com/page.html",
        )

    assert result == "inlined"
    mock_s3.put_object.assert_called_once_with(
        Bucket="bucket",
        Key="key.html",
        Body=inlined,
        ContentType="text/html",
    )


def test_process_document_dry_run_does_not_upload() -> None:
    """In dry-run mode, no S3 upload should happen."""
    original = b"<html><head><link rel='stylesheet' href='/css/s.css'></head><body></body></html>"
    inlined = b"<html><head><style>body{}</style></head><body></body></html>"

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=original))}

    mock_http = MagicMock(spec=httpx.Client)

    with patch("backfill_css_inlining.inline_css", return_value=inlined):
        result = backfill.process_document(
            mock_s3,
            mock_http,
            "doc-1",
            "key.html",
            "bucket",
            "https://court.example.com/page.html",
            dry_run=True,
        )

    assert result == "inlined"
    mock_s3.put_object.assert_not_called()


def test_process_document_handles_errors_gracefully() -> None:
    """Errors during processing should return 'error', not raise."""
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = Exception("S3 error")

    mock_http = MagicMock(spec=httpx.Client)

    result = backfill.process_document(
        mock_s3, mock_http, "doc-1", "key.html", "bucket", "https://court.example.com/page.html"
    )

    assert result == "error"
