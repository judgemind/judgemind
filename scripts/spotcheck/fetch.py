#!/usr/bin/env python3
"""Spotcheck fetcher — expand a manifest into a structured review folder.

Takes a manifest JSON (produced by sample.py) and gathers all paired
artifacts needed for review: DB records, S3 documents, and screenshots.

Usage:
    scripts/run-py.sh scripts/spotcheck/fetch.py manifest.json \\
        --output tmp/spotcheck/LA-rulings-20260328/

Each item is self-contained in its own subfolder so an LLM can review
one item at a time.

Expansion strategies are registered in STRATEGIES. To add a new entity
type, implement an ExpansionStrategy subclass and register it.
"""

# venv: scraper-framework
from __future__ import annotations

import abc
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Expansion strategy base class and registry
# ---------------------------------------------------------------------------


class ExpansionStrategy(abc.ABC):
    """Base class for entity-type-specific expansion strategies.

    Subclasses implement ``expand()`` to fetch all artifacts for a single
    entity ID and write them into ``item_dir``.
    """

    @abc.abstractmethod
    def expand(self, entity_id: str, item_dir: Path) -> dict[str, Any]:
        """Fetch artifacts for one entity and write them to item_dir.

        Args:
            entity_id: The entity ID (ruling UUID, S3 key, etc.)
            item_dir: Directory to write artifacts into (created by caller).

        Returns:
            A dict summarizing what was fetched (for logging/review.py).
        """


# Registry mapping entity type names to strategy classes.
STRATEGIES: dict[str, type[ExpansionStrategy]] = {}


def register_strategy(entity_type: str) -> Any:
    """Decorator to register an expansion strategy for an entity type."""

    def decorator(cls: type[ExpansionStrategy]) -> type[ExpansionStrategy]:
        STRATEGIES[entity_type] = cls
        return cls

    return decorator


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _run_db_query(sql: str) -> str:
    """Run a SQL query via dev-db-query.sh and return the output.

    Falls back to direct psycopg if DATABASE_URL is set (ECS environment).
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        import psycopg

        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = (
                    [desc[0] for desc in cur.description] if cur.description else []
                )
                rows = cur.fetchall()
                if not rows:
                    return json.dumps([])
                return json.dumps(
                    [dict(zip(columns, row)) for row in rows],
                    default=str,
                )
    # Local mode: use dev-db-query.sh
    repo_root = _find_repo_root()
    result = subprocess.run(
        [str(repo_root / "scripts" / "dev-db-query.sh"), sql],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"WARNING: DB query failed: {result.stderr}", file=sys.stderr)
        return "[]"
    return result.stdout


def _fetch_s3_object(s3_key: str, s3_bucket: str, dest_path: Path) -> bool:
    """Download an S3 object to a local path. Returns True on success."""
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    try:
        s3.download_file(s3_bucket, s3_key, str(dest_path))
        return True
    except ClientError as e:
        print(
            f"WARNING: Failed to download s3://{s3_bucket}/{s3_key}: {e}",
            file=sys.stderr,
        )
        return False


def _take_screenshot(url_path: str, output_path: Path) -> bool:
    """Take a screenshot via scripts/screenshot.py. Returns True on success."""
    repo_root = _find_repo_root()
    result = subprocess.run(
        [
            str(repo_root / "scripts" / "run-py.sh"),
            str(repo_root / "scripts" / "screenshot.py"),
            url_path,
            "--output",
            str(output_path),
            "--full-page",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(
            f"WARNING: Screenshot failed for {url_path}: {result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _find_repo_root() -> Path:
    """Walk up from this file to find the repo root (has CLAUDE.md + .git dir)."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "CLAUDE.md").exists() and (current / ".git").is_dir():
            return current
        current = current.parent
    # Fallback: walk from cwd
    current = Path.cwd()
    while current != current.parent:
        if (current / "CLAUDE.md").exists():
            return current
        current = current.parent
    msg = "Cannot find repo root"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Rulings expansion strategy
# ---------------------------------------------------------------------------


@register_strategy("rulings")
class RulingsStrategy(ExpansionStrategy):
    """Expand a ruling ID into DB record, original document, and screenshots."""

    def expand(self, entity_id: str, item_dir: Path) -> dict[str, Any]:
        result: dict[str, Any] = {"id": entity_id, "artifacts": []}

        # 1. Fetch DB record
        query = (
            "SELECT r.*, c.county, c.court_name, c.court_code, "
            "cs.case_number, cs.case_title, "
            "d.s3_key, d.s3_bucket, d.format AS doc_format "
            "FROM rulings r "
            "JOIN courts c ON r.court_id = c.id "
            "JOIN cases cs ON r.case_id = cs.id "
            "JOIN documents d ON r.document_id = d.id "
            f"WHERE r.id = '{entity_id}'"
        )
        db_output = _run_db_query(query)
        db_record_path = item_dir / "db_record.json"
        db_record_path.write_text(db_output, encoding="utf-8")
        result["artifacts"].append("db_record.json")

        # Parse the record to get S3 info and case_id
        records = json.loads(db_output)
        if not records:
            print(
                f"WARNING: No DB record found for ruling {entity_id}",
                file=sys.stderr,
            )
            return result

        record = records[0]
        s3_key = record.get("s3_key", "")
        s3_bucket = record.get("s3_bucket", "")
        doc_format = record.get("doc_format", "html")
        case_id = record.get("case_id", "")

        # 2. Fetch original document from S3
        if s3_key and s3_bucket:
            ext = "pdf" if doc_format == "pdf" else "html"
            original_path = item_dir / f"original.{ext}"
            if _fetch_s3_object(s3_key, s3_bucket, original_path):
                result["artifacts"].append(f"original.{ext}")

        # 3. Take ruling detail screenshot
        ruling_screenshot_path = item_dir / "ruling_screenshot.png"
        if _take_screenshot(f"/rulings/{entity_id}", ruling_screenshot_path):
            result["artifacts"].append("ruling_screenshot.png")

        # 4. Take case detail screenshot
        if case_id:
            case_screenshot_path = item_dir / "case_screenshot.png"
            if _take_screenshot(f"/cases/{case_id}", case_screenshot_path):
                result["artifacts"].append("case_screenshot.png")

        return result


# ---------------------------------------------------------------------------
# Originals expansion strategy
# ---------------------------------------------------------------------------


@register_strategy("originals")
class OriginalsStrategy(ExpansionStrategy):
    """Expand an original document S3 key into doc + derived rulings."""

    def expand(self, entity_id: str, item_dir: Path) -> dict[str, Any]:
        result: dict[str, Any] = {"id": entity_id, "artifacts": []}

        # 1. Fetch original document from S3
        bucket = os.environ.get("S3_BUCKET", "judgemind-documents-dev")
        ext = "pdf" if entity_id.endswith(".pdf") else "html"
        original_path = item_dir / f"original.{ext}"
        if _fetch_s3_object(entity_id, bucket, original_path):
            result["artifacts"].append(f"original.{ext}")

        # 2. Fetch all derived rulings from DB
        # The S3 key is stored in documents.s3_key; rulings link via document_id
        query = (
            "SELECT r.id::text AS ruling_id, r.ruling_text, r.outcome::text, "
            "r.motion_type, r.hearing_date::text, r.department, "
            "cs.case_number, cs.case_title "
            "FROM rulings r "
            "JOIN documents d ON r.document_id = d.id "
            "JOIN cases cs ON r.case_id = cs.id "
            f"WHERE d.s3_key = '{entity_id}'"
        )
        db_output = _run_db_query(query)
        derived_path = item_dir / "derived_rulings.json"
        derived_path.write_text(db_output, encoding="utf-8")
        result["artifacts"].append("derived_rulings.json")

        # 3. Take screenshots for each derived ruling
        derived_rulings = json.loads(db_output)
        if derived_rulings:
            screenshots_dir = item_dir / "ruling_screenshots"
            screenshots_dir.mkdir(exist_ok=True)
            for ruling in derived_rulings:
                ruling_id = ruling.get("ruling_id", "")
                if ruling_id:
                    screenshot_path = screenshots_dir / f"{ruling_id}.png"
                    if _take_screenshot(f"/rulings/{ruling_id}", screenshot_path):
                        result["artifacts"].append(
                            f"ruling_screenshots/{ruling_id}.png"
                        )

        return result


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def fetch_manifest(manifest: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Process a manifest and expand all entities into output_dir.

    Args:
        manifest: The manifest dict (entity, county, ids, etc.)
        output_dir: Root directory for output.

    Returns:
        A summary dict with results per entity.
    """
    entity_type = manifest["entity"]

    if entity_type not in STRATEGIES:
        print(
            f"ERROR: Unknown entity type '{entity_type}'. "
            f"Available: {sorted(STRATEGIES.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)

    strategy = STRATEGIES[entity_type]()
    ids = manifest["ids"]

    # Create output structure
    output_dir.mkdir(parents=True, exist_ok=True)
    items_dir = output_dir / "items"
    items_dir.mkdir(exist_ok=True)

    # Copy manifest into output
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Expand each entity
    summary: dict[str, Any] = {
        "entity": entity_type,
        "county": manifest.get("county", ""),
        "total": len(ids),
        "items": [],
    }

    for i, entity_id in enumerate(ids):
        # Use a safe directory name (first 8 chars of UUID or sanitized key)
        safe_name = _safe_dirname(entity_id)
        item_dir = items_dir / safe_name
        item_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"[{i + 1}/{len(ids)}] Expanding {entity_type} {safe_name}...",
            file=sys.stderr,
        )

        item_result = strategy.expand(entity_id, item_dir)
        summary["items"].append(item_result)

    return summary


def _safe_dirname(entity_id: str) -> str:
    """Create a filesystem-safe directory name from an entity ID.

    For UUIDs, returns the full UUID.
    For S3 keys, replaces path separators with underscores.
    """
    # If it looks like a UUID (36 chars with hyphens), keep it as-is
    if len(entity_id) == 36 and entity_id.count("-") == 4:
        return entity_id
    # For S3 keys, sanitize
    return entity_id.replace("/", "_").replace(" ", "_")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expand a spotcheck manifest into a structured review folder",
    )
    parser.add_argument(
        "manifest",
        help="Path to manifest JSON file (from sample.py)",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Output directory for expanded artifacts",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"ERROR: Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_dir = Path(args.output)

    # Warn if output dir exists
    if output_dir.exists():
        print(
            f"WARNING: Output directory already exists: {output_dir}. "
            "Existing files may be overwritten.",
            file=sys.stderr,
        )

    summary = fetch_manifest(manifest, output_dir)

    # Write summary
    summary_path = output_dir / "fetch_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(
        f"\nFetch complete. {summary['total']} items expanded to {output_dir}",
        file=sys.stderr,
    )
    print(f"Summary: {summary_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
