#!/usr/bin/env python3
"""Local S3 cache — bulk sync and CLI utilities."""
# venv: scraper-framework
#
# Content-addressed keys mean a local file is always correct if it exists.
# Cache dir: /tmp/judgemind-archive/ (mirrors S3 key structure)
#
# CLI: scripts/run-py.sh scripts/s3_cache.py sync [--prefix ca/orange/]
#      scripts/run-py.sh scripts/s3_cache.py get <key>
#      scripts/run-py.sh scripts/s3_cache.py stats
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")
CACHE_DIR = Path(os.environ.get("S3_CACHE_DIR", "/tmp/judgemind-archive"))


class LocalS3Cache:
    """Bulk sync and CLI operations for the local S3 cache.

    For transparent caching inside framework code, use
    framework.s3_cache.make_s3_client() instead — it returns a CachedS3Client
    that acts as a drop-in boto3 S3 client replacement.
    """

    def __init__(
        self,
        bucket: str = BUCKET,
        cache_dir: Path = CACHE_DIR,
        s3_client: object | None = None,
    ) -> None:
        self.bucket = bucket
        self.cache_dir = cache_dir
        self._s3 = s3_client or boto3.client("s3")

    def get_path(self, key: str) -> Path:
        """Return local path for an S3 key, fetching if not cached."""
        local = self.cache_dir / key
        if local.exists():
            return local
        self._fetch(key, local)
        return local

    def has(self, key: str) -> bool:
        """Check if a key is cached locally (no S3 call)."""
        return (self.cache_dir / key).exists()

    def _fetch(self, key: str, local: Path) -> None:
        """Download a single S3 object to local cache."""
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._s3.download_file(self.bucket, key, str(local))
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                raise FileNotFoundError(f"s3://{self.bucket}/{key} not found") from exc
            raise

    def sync(
        self,
        prefix: str = "ca/",
        *,
        progress: bool = True,
    ) -> dict[str, int]:
        """Bulk sync S3 objects to local cache. Returns stats dict.

        Skips objects that already exist locally (content-addressed = correct).
        """
        paginator = self._s3.get_paginator("list_objects_v2")
        stats = {"total": 0, "cached": 0, "downloaded": 0, "bytes": 0, "downloaded_bytes": 0}

        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                size = obj["Size"]
                stats["total"] += 1
                stats["bytes"] += size

                local = self.cache_dir / key
                if local.exists():
                    stats["cached"] += 1
                    continue

                local.parent.mkdir(parents=True, exist_ok=True)
                self._s3.download_file(self.bucket, key, str(local))
                stats["downloaded"] += 1
                stats["downloaded_bytes"] += size

                if progress and stats["downloaded"] % 100 == 0:
                    print(
                        f"  Downloaded {stats['downloaded']} objects "
                        f"({_fmt_bytes(stats['downloaded_bytes'])}), "
                        f"{stats['cached']} already cached...",
                        file=sys.stderr,
                    )

        return stats

    def cache_stats(self) -> dict[str, int]:
        """Report local cache size and file counts by extension."""
        by_ext: dict[str, int] = {}
        total_size = 0
        total_files = 0
        for path in self.cache_dir.rglob("*"):
            if path.is_file():
                ext = path.suffix or "(none)"
                by_ext[ext] = by_ext.get(ext, 0) + 1
                total_size += path.stat().st_size
                total_files += 1
        return {"files": total_files, "size": total_size, "by_ext": by_ext}


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def cmd_sync(args: argparse.Namespace) -> None:
    cache = LocalS3Cache()
    prefix = args.prefix
    print(f"Syncing s3://{BUCKET}/{prefix} → {CACHE_DIR}/", file=sys.stderr)
    stats = cache.sync(prefix=prefix)
    print(
        f"\nDone: {stats['total']} objects ({_fmt_bytes(stats['bytes'])} total), "
        f"{stats['downloaded']} downloaded ({_fmt_bytes(stats['downloaded_bytes'])}), "
        f"{stats['cached']} already cached.",
        file=sys.stderr,
    )


def cmd_get(args: argparse.Namespace) -> None:
    cache = LocalS3Cache()
    path = cache.get_path(args.key)
    print(path)


def cmd_stats(args: argparse.Namespace) -> None:
    cache = LocalS3Cache()
    stats = cache.cache_stats()
    print(f"Cache dir: {CACHE_DIR}")
    print(f"Total files: {stats['files']}")
    print(f"Total size: {_fmt_bytes(stats['size'])}")
    if stats["by_ext"]:
        print("By extension:")
        for ext, count in sorted(stats["by_ext"].items(), key=lambda x: -x[1]):
            print(f"  {ext}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local S3 cache for judgemind documents")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Bulk sync S3 objects to local cache")
    p_sync.add_argument("--prefix", default="ca/", help="S3 prefix to sync (default: ca/)")
    p_sync.set_defaults(func=cmd_sync)

    p_get = sub.add_parser("get", help="Fetch a single S3 key (lazy cache)")
    p_get.add_argument("key", help="S3 key to fetch")
    p_get.set_defaults(func=cmd_get)

    p_stats = sub.add_parser("stats", help="Show local cache statistics")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
