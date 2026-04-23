"""Tests for S3 content-hash integrity helpers."""

from __future__ import annotations

import hashlib
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from framework.s3_integrity import (
    S3MislabelError,
    assert_key_matches_bytes,
    extract_filename_hash,
    verify_key_matches_bytes,
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# extract_filename_hash
# ---------------------------------------------------------------------------


def test_extract_filename_hash_valid() -> None:
    content_hash = "a" * 64
    key = f"ca/orange/superior_court/raw/{content_hash}.html"
    assert extract_filename_hash(key) == content_hash


def test_extract_filename_hash_invalid_returns_none() -> None:
    assert extract_filename_hash("ca/orange/superior_court/raw/some_other_format.html") is None
    assert extract_filename_hash("ca/orange/superior_court/documents/abc123.html") is None
    assert extract_filename_hash("not_a_key_at_all") is None


# ---------------------------------------------------------------------------
# verify_key_matches_bytes
# ---------------------------------------------------------------------------


def test_verify_match() -> None:
    """HEAD returns matching metadata content-hash; GET returns bytes whose sha256 matches."""
    content = b"hello world"
    content_hash = _sha256_hex(content)
    key = f"ca/orange/superior_court/raw/{content_hash}.html"

    mock_client = MagicMock()
    mock_client.head_object.return_value = {
        "Metadata": {"content-hash": content_hash},
    }
    mock_client.get_object.return_value = {"Body": BytesIO(content)}

    result = verify_key_matches_bytes(mock_client, "test-bucket", key)

    assert result is True
    mock_client.head_object.assert_called_once_with(Bucket="test-bucket", Key=key)
    mock_client.get_object.assert_called_once_with(Bucket="test-bucket", Key=key)


def test_verify_filename_vs_bytes_mismatch() -> None:
    """Bytes hash differs from filename hash — raises S3MislabelError."""
    filename_hash = "a" * 64
    key = f"ca/orange/superior_court/raw/{filename_hash}.html"

    actual_content = b"different content"
    actual_hash = _sha256_hex(actual_content)
    assert actual_hash != filename_hash

    mock_client = MagicMock()
    mock_client.head_object.return_value = {
        # metadata matches filename (not the bytes)
        "Metadata": {"content-hash": filename_hash},
    }
    mock_client.get_object.return_value = {"Body": BytesIO(actual_content)}

    with pytest.raises(S3MislabelError) as exc_info:
        verify_key_matches_bytes(mock_client, "test-bucket", key)

    assert filename_hash in str(exc_info.value) or actual_hash in str(exc_info.value)


def test_verify_metadata_vs_bytes_mismatch() -> None:
    """Metadata content-hash differs from actual bytes hash — raises S3MislabelError."""
    actual_content = b"the real content"
    actual_hash = _sha256_hex(actual_content)
    wrong_metadata_hash = "b" * 64
    assert wrong_metadata_hash != actual_hash

    key = f"ca/orange/superior_court/raw/{actual_hash}.html"

    mock_client = MagicMock()
    mock_client.head_object.return_value = {
        "Metadata": {"content-hash": wrong_metadata_hash},
    }
    mock_client.get_object.return_value = {"Body": BytesIO(actual_content)}

    with pytest.raises(S3MislabelError):
        verify_key_matches_bytes(mock_client, "test-bucket", key)


def test_verify_missing_metadata() -> None:
    """HEAD returns no Metadata.content-hash — raises S3MislabelError."""
    content_hash = "c" * 64
    key = f"ca/orange/superior_court/raw/{content_hash}.html"

    mock_client = MagicMock()
    mock_client.head_object.return_value = {
        "Metadata": {},  # no content-hash key
    }

    with pytest.raises(S3MislabelError):
        verify_key_matches_bytes(mock_client, "test-bucket", key)


def test_verify_missing_object() -> None:
    """HEAD returns 404 — returns False (key does not exist)."""
    content_hash = "d" * 64
    key = f"ca/orange/superior_court/raw/{content_hash}.html"

    mock_client = MagicMock()
    mock_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "HeadObject",
    )

    result = verify_key_matches_bytes(mock_client, "test-bucket", key)

    assert result is False


def test_verify_unparseable_key() -> None:
    """Key doesn't match .../raw/<hex64>.<ext> — raises S3MislabelError or ValueError."""
    key = "ca/orange/superior_court/raw/not_a_hash.html"

    mock_client = MagicMock()
    mock_client.head_object.return_value = {
        "Metadata": {"content-hash": "e" * 64},
    }

    with pytest.raises((S3MislabelError, ValueError)):
        verify_key_matches_bytes(mock_client, "test-bucket", key)


# ---------------------------------------------------------------------------
# assert_key_matches_bytes
# ---------------------------------------------------------------------------


def test_assert_raises_on_mismatch() -> None:
    """assert_key_matches_bytes raises S3MislabelError when filename and bytes hashes differ."""
    filename_hash = "a" * 64
    key = f"ca/orange/superior_court/raw/{filename_hash}.html"

    actual_content = b"different content"
    actual_hash = _sha256_hex(actual_content)
    assert actual_hash != filename_hash

    mock_client = MagicMock()
    mock_client.head_object.return_value = {
        "Metadata": {"content-hash": filename_hash},
    }
    mock_client.get_object.return_value = {"Body": BytesIO(actual_content)}

    with pytest.raises(S3MislabelError):
        assert_key_matches_bytes(mock_client, "test-bucket", key)


def test_assert_raises_when_object_missing() -> None:
    """assert_key_matches_bytes raises S3MislabelError when the object does not exist."""
    content_hash = "f" * 64
    key = f"ca/orange/superior_court/raw/{content_hash}.html"

    mock_client = MagicMock()
    mock_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "HeadObject",
    )

    with pytest.raises(S3MislabelError):
        assert_key_matches_bytes(mock_client, "test-bucket", key)


def test_assert_passes_on_valid_object() -> None:
    """assert_key_matches_bytes does not raise when all hashes match."""
    content = b"valid content"
    content_hash = _sha256_hex(content)
    key = f"ca/orange/superior_court/raw/{content_hash}.html"

    mock_client = MagicMock()
    mock_client.head_object.return_value = {
        "Metadata": {"content-hash": content_hash},
    }
    mock_client.get_object.return_value = {"Body": BytesIO(content)}

    # Should not raise
    assert_key_matches_bytes(mock_client, "test-bucket", key)
