"""Tests for S3 archival utility."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from framework.models import CapturedDocument, ContentFormat
from framework.storage import S3Archiver, build_s3_key


def _make_doc(**kwargs: object) -> CapturedDocument:
    defaults = dict(
        scraper_id="test-scraper",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url="https://example.com/ruling",
        capture_timestamp=datetime(2026, 3, 1, 10, 0, 0),
        content_format=ContentFormat.HTML,
        raw_content=b"<html>ruling</html>",
        content_hash="abc123def456",
    )
    defaults.update(kwargs)
    return CapturedDocument(**defaults)


def test_build_s3_key_structure() -> None:
    doc = _make_doc()
    key = build_s3_key(doc)
    assert key == "ca/los_angeles/superior_court/raw/abc123def456.html"


def test_build_s3_key_pdf() -> None:
    doc = _make_doc(content_format=ContentFormat.PDF)
    key = build_s3_key(doc)
    assert key.endswith(".pdf")


def test_archive_uploads_when_not_exists() -> None:
    from botocore.exceptions import ClientError

    mock_client = MagicMock()
    # HeadObject returns 404 → object doesn't exist → should upload
    mock_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    archiver = S3Archiver(bucket="test-bucket", s3_client=mock_client)
    doc = _make_doc()

    key = archiver.archive(doc)

    mock_client.head_object.assert_called_once()
    mock_client.put_object.assert_called_once()
    call_kwargs = mock_client.put_object.call_args.kwargs
    assert call_kwargs["Bucket"] == "test-bucket"
    assert call_kwargs["Body"] == b"<html>ruling</html>"
    assert call_kwargs["ContentType"] == "text/html"
    assert key == call_kwargs["Key"]


def test_archive_skips_upload_when_exists() -> None:
    mock_client = MagicMock()
    # HeadObject succeeds → object already exists → skip upload
    mock_client.head_object.return_value = {}
    archiver = S3Archiver(bucket="test-bucket", s3_client=mock_client)
    doc = _make_doc()

    key = archiver.archive(doc)

    mock_client.head_object.assert_called_once()
    mock_client.put_object.assert_not_called()
    assert key == "ca/los_angeles/superior_court/raw/abc123def456.html"


def test_archive_raises_on_s3_error() -> None:
    from botocore.exceptions import ClientError

    mock_client = MagicMock()
    # HeadObject returns 404 (not exists), but PutObject fails
    mock_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )
    mock_client.put_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": ""}}, "PutObject"
    )
    archiver = S3Archiver(bucket="missing-bucket", s3_client=mock_client)
    doc = _make_doc()

    with pytest.raises(ClientError):
        archiver.archive(doc)
