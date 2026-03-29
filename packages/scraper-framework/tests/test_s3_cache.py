"""Tests for S3 cache client (CachedS3Client and local-only mode)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from framework.s3_cache import CachedS3Client


class TestCachedS3ClientLocalOnly:
    """Tests for local-only mode (no S3 contact)."""

    def test_get_object_hits_cache(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "test").mkdir()
        (cache_dir / "test" / "doc.pdf").write_bytes(b"pdf content")

        client = CachedS3Client(cache_dir=cache_dir, local_only=True)
        result = client.get_object(Bucket="b", Key="test/doc.pdf")
        assert result["Body"].read() == b"pdf content"

    def test_get_object_miss_raises(self, tmp_path: Path) -> None:
        client = CachedS3Client(cache_dir=tmp_path, local_only=True)
        with pytest.raises(ClientError):
            client.get_object(Bucket="b", Key="nonexistent.pdf")

    def test_put_object_writes_locally(self, tmp_path: Path) -> None:
        client = CachedS3Client(cache_dir=tmp_path, local_only=True)
        client.put_object(Bucket="b", Key="test/doc.pdf", Body=b"content")
        assert (tmp_path / "test" / "doc.pdf").read_bytes() == b"content"

    def test_head_object_cached(self, tmp_path: Path) -> None:
        (tmp_path / "doc.pdf").write_bytes(b"content")
        client = CachedS3Client(cache_dir=tmp_path, local_only=True)
        result = client.head_object(Bucket="b", Key="doc.pdf")
        assert result["ContentLength"] == 7

    def test_head_object_miss_raises(self, tmp_path: Path) -> None:
        client = CachedS3Client(cache_dir=tmp_path, local_only=True)
        with pytest.raises(ClientError):
            client.head_object(Bucket="b", Key="missing.pdf")

    def test_getattr_raises_in_local_only(self, tmp_path: Path) -> None:
        client = CachedS3Client(cache_dir=tmp_path, local_only=True)
        with pytest.raises(AttributeError, match="local-only mode"):
            client.get_paginator("list_objects_v2")

    def test_download_file_from_cache(self, tmp_path: Path) -> None:
        (tmp_path / "src.pdf").write_bytes(b"cached pdf")
        client = CachedS3Client(cache_dir=tmp_path, local_only=True)
        dest = tmp_path / "output.pdf"
        client.download_file("b", "src.pdf", str(dest))
        assert dest.read_bytes() == b"cached pdf"

    def test_download_file_miss_raises(self, tmp_path: Path) -> None:
        client = CachedS3Client(cache_dir=tmp_path, local_only=True)
        with pytest.raises(FileNotFoundError):
            client.download_file("b", "missing.pdf", "/tmp/out.pdf")


class TestCachedS3ClientWriteThrough:
    """Tests for write-through mode (S3 + local cache)."""

    def test_get_object_cache_hit_skips_s3(self, tmp_path: Path) -> None:
        (tmp_path / "doc.html").write_bytes(b"<html>cached</html>")
        mock_s3 = MagicMock()
        client = CachedS3Client(cache_dir=tmp_path, s3_client=mock_s3)
        result = client.get_object(Bucket="b", Key="doc.html")
        assert result["Body"].read() == b"<html>cached</html>"
        mock_s3.get_object.assert_not_called()

    def test_get_object_cache_miss_fetches_and_caches(self, tmp_path: Path) -> None:
        import io

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": io.BytesIO(b"from s3")}
        client = CachedS3Client(cache_dir=tmp_path, s3_client=mock_s3)
        result = client.get_object(Bucket="b", Key="new/doc.pdf")
        assert result["Body"].read() == b"from s3"
        assert (tmp_path / "new" / "doc.pdf").read_bytes() == b"from s3"

    def test_put_object_writes_to_s3_and_cache(self, tmp_path: Path) -> None:
        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {}
        client = CachedS3Client(cache_dir=tmp_path, s3_client=mock_s3)
        client.put_object(Bucket="b", Key="test/doc.pdf", Body=b"content")
        mock_s3.put_object.assert_called_once()
        assert (tmp_path / "test" / "doc.pdf").read_bytes() == b"content"

    def test_head_object_cache_hit_skips_s3(self, tmp_path: Path) -> None:
        (tmp_path / "doc.pdf").write_bytes(b"x" * 100)
        mock_s3 = MagicMock()
        client = CachedS3Client(cache_dir=tmp_path, s3_client=mock_s3)
        result = client.head_object(Bucket="b", Key="doc.pdf")
        assert result["ContentLength"] == 100
        mock_s3.head_object.assert_not_called()
