#!/usr/bin/env python3
"""Migrate S3 document archive from date-stamped to content-addressed keys.

Old scheme: {state}/{county}/{court}/raw/{YYYY}/{MM}/{DD}/{document_id}.{ext}
New scheme: {state}/{county}/{court}/raw/{content_hash}.{ext}

Steps:
  1. For each document row, compute the new content-addressed key.
  2. Copy the S3 object from old key to new key (if not already there).
  3. Update the DB row's s3_key.
  4. Optionally delete old keys and orphaned objects.

Idempotent: safe to re-run if interrupted.

Usage:
  scripts/ecs-run-task.sh scripts/migrate_s3_keys.py -- --dry-run
  scripts/ecs-run-task.sh scripts/migrate_s3_keys.py
  scripts/ecs-run-task.sh scripts/migrate_s3_keys.py -- --cleanup-orphans
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

# Extension map matching storage.py — DB format enum values map directly.
EXT_MAP = {"html": "html", "pdf": "pdf", "docx": "docx", "txt": "txt"}

# Matches new content-addressed keys: .../raw/{hex64}.{ext}
NEW_KEY_PATTERN = re.compile(r".*/raw/[0-9a-f]{64}\.\w+$")


def compute_new_key(old_key: str, content_hash: str, fmt: str) -> str:
    """Derive the new content-addressed key from the old key's prefix."""
    # Old key: ca/orange/superior_court/raw/2026/03/17/uuid.pdf
    # Extract everything up to and including /raw/
    raw_idx = old_key.index("/raw/")
    prefix = old_key[: raw_idx + len("/raw/")]
    ext = EXT_MAP.get(fmt, "bin")
    return f"{prefix}{content_hash}.{ext}"


def migrate_documents(
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
            "SELECT id, s3_key, content_hash, format::text "
            "FROM documents WHERE s3_key IS NOT NULL"
        )
        rows = cur.fetchall()

    print(f"Found {len(rows)} document rows to check.")

    for doc_id, old_key, content_hash, fmt in rows:
        if not content_hash:
            print(f"  SKIP {doc_id}: no content_hash")
            errors += 1
            continue

        new_key = compute_new_key(old_key, content_hash, fmt)

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
                    "UPDATE documents SET s3_key = %s WHERE id = %s",
                    (new_key, doc_id),
                )
            conn.commit()
            migrated += 1
            if migrated % 50 == 0:
                print(f"  Migrated {migrated} documents...")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                # Old key doesn't exist — check if new key already exists
                # (previous partial run may have copied but not updated DB).
                try:
                    s3.head_object(Bucket=BUCKET, Key=new_key)
                    # New key exists — just update DB.
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE documents SET s3_key = %s WHERE id = %s",
                            (new_key, doc_id),
                        )
                    conn.commit()
                    migrated += 1
                except ClientError:
                    print(f"  ERROR {doc_id}: old key missing, new key missing: {old_key}")
                    errors += 1
            else:
                print(f"  ERROR {doc_id}: {exc}")
                errors += 1

    return migrated, already_done, errors


def cleanup_old_keys(s3: object, conn: psycopg.Connection, *, dry_run: bool) -> int:
    """Delete old date-stamped S3 keys that are no longer referenced by any DB row."""
    # Get all current s3_keys from the DB.
    with conn.cursor() as cur:
        cur.execute("SELECT s3_key FROM documents WHERE s3_key IS NOT NULL")
        db_keys = {row[0] for row in cur.fetchall()}

    print(f"\n{len(db_keys)} S3 keys referenced in DB.")

    # List all objects in the bucket under ca/ (our only state for now).
    paginator = s3.get_paginator("list_objects_v2")
    all_keys: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix="ca/"):
        for obj in page.get("Contents", []):
            all_keys.append(obj["Key"])

    print(f"{len(all_keys)} total S3 objects under ca/.")

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
        help="Delete orphaned S3 objects (old date-stamped keys not in DB)",
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

    # Step 1: Migrate document rows.
    print("=== Migrating document S3 keys ===")
    migrated, already_done, errors = migrate_documents(s3, conn, dry_run=args.dry_run)
    print(f"\nMigration complete: {migrated} migrated, {already_done} already done, {errors} errors.")

    if errors > 0:
        print("WARNING: Some documents had errors. Fix before cleanup.")
        if not args.dry_run:
            sys.exit(1)

    # Step 2: Clean up orphans (only if requested).
    if args.cleanup_orphans:
        print("\n=== Cleaning up orphaned S3 objects ===")
        deleted = cleanup_old_keys(s3, conn, dry_run=args.dry_run)
        print(f"Orphan cleanup: {deleted} objects {'would be ' if args.dry_run else ''}deleted.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
