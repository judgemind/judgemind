#!/usr/bin/env python3
"""Migrate court directory S3 keys from timestamp-based to content-addressed.

Old scheme: directories/{court_id}/{YYYYMMDDTHHMMSSz}.html
New scheme: directories/{court_id}/{content_hash}.html

Steps:
  1. For each court_directory_snapshots row, compute the new content-addressed key.
  2. Copy the S3 object from old key to new key (if not already there).
  3. Update the DB row's s3_key.
  4. Optionally delete old keys and orphaned objects.

Idempotent: safe to re-run if interrupted.

Usage:
  scripts/ecs-run-task.sh scripts/migrate_directory_s3_keys.py -- --dry-run
  scripts/ecs-run-task.sh scripts/migrate_directory_s3_keys.py
  scripts/ecs-run-task.sh scripts/migrate_directory_s3_keys.py -- --cleanup-orphans
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import re
import sys

import boto3
import psycopg
from botocore.exceptions import ClientError

BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Matches new content-addressed keys: directories/{court_id}/{hex64}.html
NEW_KEY_PATTERN = re.compile(r"directories/[^/]+/[0-9a-f]{64}\.html$")


def compute_new_key(old_key: str, content_hash: str) -> str:
    """Derive the new content-addressed key from the old key's court_id prefix."""
    # Old key: directories/ca_los_angeles/20260115T120000Z.html
    # New key: directories/ca_los_angeles/{content_hash}.html
    parts = old_key.split("/")
    # parts[0] = "directories", parts[1] = court_id, parts[2] = timestamp.html
    court_id = parts[1]
    return f"directories/{court_id}/{content_hash}.html"


def migrate_snapshots(
    s3: object, conn: psycopg.Connection, *, dry_run: bool
) -> tuple[int, int, int]:
    """Copy S3 objects to new keys and update DB rows.

    Returns (migrated, already_done, errors).
    """
    migrated = 0
    already_done = 0
    errors = 0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, s3_key, content_hash "
            "FROM court_directory_snapshots WHERE s3_key IS NOT NULL"
        )
        rows = cur.fetchall()

    print(f"Found {len(rows)} directory snapshot rows to check.")

    for row_id, old_key, content_hash in rows:
        if not content_hash:
            print(f"  SKIP {row_id}: no content_hash")
            errors += 1
            continue

        new_key = compute_new_key(old_key, content_hash)

        if old_key == new_key:
            already_done += 1
            continue

        if dry_run:
            print(f"  WOULD migrate {old_key} -> {new_key}")
            migrated += 1
            continue

        try:
            # Copy object to new key (S3 CopyObject is atomic).
            s3.copy_object(
                Bucket=BUCKET,
                CopySource={"Bucket": BUCKET, "Key": old_key},
                Key=new_key,
            )
            # Update DB row.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE court_directory_snapshots SET s3_key = %s WHERE id = %s",
                    (new_key, row_id),
                )
            conn.commit()
            migrated += 1
            if migrated % 50 == 0:
                print(f"  Migrated {migrated} snapshots...")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                # Old key doesn't exist — check if new key already exists
                # (previous partial run may have copied but not updated DB).
                try:
                    s3.head_object(Bucket=BUCKET, Key=new_key)
                    # New key exists — just update DB.
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE court_directory_snapshots SET s3_key = %s WHERE id = %s",
                            (new_key, row_id),
                        )
                    conn.commit()
                    migrated += 1
                except ClientError:
                    print(
                        f"  ERROR {row_id}: old key missing, new key missing: {old_key}"
                    )
                    errors += 1
            else:
                print(f"  ERROR {row_id}: {exc}")
                errors += 1

    return migrated, already_done, errors


def cleanup_old_keys(s3: object, conn: psycopg.Connection, *, dry_run: bool) -> int:
    """Delete old timestamp-based S3 keys that are no longer referenced by any DB row."""
    # Get all current s3_keys from the DB.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s3_key FROM court_directory_snapshots WHERE s3_key IS NOT NULL"
        )
        db_keys = {row[0] for row in cur.fetchall()}

    print(f"\n{len(db_keys)} S3 keys referenced in DB.")

    # List all objects in the bucket under directories/ prefix.
    paginator = s3.get_paginator("list_objects_v2")
    all_keys: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix="directories/"):
        for obj in page.get("Contents", []):
            all_keys.append(obj["Key"])

    print(f"{len(all_keys)} total S3 objects under directories/.")

    orphans = [k for k in all_keys if k not in db_keys]
    print(f"{len(orphans)} orphaned S3 objects to delete.")

    if dry_run:
        for k in orphans[:20]:
            print(f"  WOULD delete: {k}")
        if len(orphans) > 20:
            print(f"  ... and {len(orphans) - 20} more")
        return len(orphans)

    # Delete in batches of 1000 (S3 delete_objects limit).
    deleted = 0
    for i in range(0, len(orphans), 1000):
        batch = orphans[i : i + 1000]
        s3.delete_objects(
            Bucket=BUCKET,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        deleted += len(batch)
        print(f"  Deleted {deleted}/{len(orphans)} orphans...")

    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--cleanup-orphans",
        action="store_true",
        help="Delete orphaned S3 objects (old timestamp-based keys not in DB)",
    )
    args = parser.parse_args()

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set. Run via ecs-run-task.sh.", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client("s3")
    conn = psycopg.connect(DATABASE_URL)

    print(f"Bucket: {BUCKET}")
    print(f"Dry run: {args.dry_run}")
    print()

    # Step 1: Migrate directory snapshot rows.
    print("=== Migrating directory snapshot S3 keys ===")
    migrated, already_done, errors = migrate_snapshots(s3, conn, dry_run=args.dry_run)
    print(
        f"\nMigration complete: {migrated} migrated, "
        f"{already_done} already done, {errors} errors."
    )

    if errors > 0:
        print("WARNING: Some snapshots had errors. Fix before cleanup.")
        if not args.dry_run:
            sys.exit(1)

    # Step 2: Clean up orphans (only if requested).
    if args.cleanup_orphans:
        print("\n=== Cleaning up orphaned S3 objects ===")
        deleted = cleanup_old_keys(s3, conn, dry_run=args.dry_run)
        print(
            f"Orphan cleanup: {deleted} objects "
            f"{'would be ' if args.dry_run else ''}deleted."
        )

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
