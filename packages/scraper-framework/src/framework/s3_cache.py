"""Local S3 cache with fetch-through for content-addressed keys.

Content-addressed keys mean a local file is always correct if it exists —
no invalidation, no TTL, no checksums needed.

Usage:
    # In any code that creates an S3 client:
    from framework.s3_cache import make_s3_client
    s3 = make_s3_client()  # cached if S3_CACHE_DIR is set, else plain boto3

    # The returned object is a drop-in replacement for boto3.client("s3").
"""

from __future__ import annotations

import io
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import boto3

logger = logging.getLogger(__name__)


class CachedS3Client:
    """Drop-in wrapper for boto3 S3 client with local disk caching.

    Intercepts get_object, put_object, and head_object to use a local cache.
    All other methods pass through to the underlying boto3 client.
    """

    def __init__(self, cache_dir: Path, s3_client: Any | None = None) -> None:
        self._cache_dir = cache_dir
        self._s3 = s3_client or boto3.client("s3")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info("S3 local cache enabled at %s", cache_dir)

    def get_object(self, **kwargs: Any) -> dict:
        """Fetch from local cache if available, otherwise from S3 and cache."""
        key = str(kwargs.get("Key", ""))
        local = self._cache_dir / key

        if local.exists():
            logger.debug("Cache hit: %s", key)
            return {"Body": io.BytesIO(local.read_bytes())}

        logger.debug("Cache miss: %s — fetching from S3", key)
        response = self._s3.get_object(**kwargs)
        body = response["Body"].read()
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(body)
        response["Body"] = io.BytesIO(body)
        return response

    def put_object(self, **kwargs: Any) -> dict:
        """Write to S3 and also cache locally."""
        result = self._s3.put_object(**kwargs)
        key = str(kwargs.get("Key", ""))
        body = kwargs.get("Body", b"")
        if key and isinstance(body, (bytes, bytearray)):
            local = self._cache_dir / key
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(bytes(body))
        return result

    def head_object(self, **kwargs: Any) -> dict:
        """Check local cache first. If cached, skip the S3 call."""
        key = str(kwargs.get("Key", ""))
        local = self._cache_dir / key
        if local.exists():
            return {"ContentLength": local.stat().st_size}
        return self._s3.head_object(**kwargs)

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        """Download to a specific path, caching along the way."""
        local = self._cache_dir / key
        if local.exists():
            shutil.copy2(str(local), filename)
            return
        self._s3.download_file(bucket, key, filename)
        if str(local) != filename:
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(filename, str(local))

    def __getattr__(self, name: str) -> Any:
        """Pass through any other method to the underlying boto3 client."""
        return getattr(self._s3, name)


def make_s3_client(s3_client: Any | None = None) -> Any:
    """Factory: returns a CachedS3Client if S3_CACHE_DIR is set, else plain boto3.

    Use this everywhere instead of boto3.client("s3") to get transparent caching.
    In production (no S3_CACHE_DIR), returns a plain boto3 client with zero overhead.
    """
    cache_dir = os.environ.get("S3_CACHE_DIR", "")
    inner = s3_client or boto3.client("s3")
    if cache_dir:
        return CachedS3Client(cache_dir=Path(cache_dir), s3_client=inner)
    return inner
