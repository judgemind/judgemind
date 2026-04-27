#!/usr/bin/env python3
# venv: scraper-framework
"""Spotcheck fetcher — expand a manifest into a structured review folder.

Takes a manifest JSON (produced by sample.py) and gathers all paired
artifacts needed for review: DB records, S3 documents, and screenshots.

Usage (originals — S3 + DB, run locally or via ECS):
    scripts/run-py.sh scripts/spotcheck/fetch.py manifest.json \\
        --output tmp/spotcheck/OC-originals-20260328/

Usage (rulings — requires DB, run via ECS for best reliability):
    scripts/ecs-run-task.sh scripts/spotcheck/fetch.py -- \\
        manifest.json --output /tmp/spotcheck-output/

Each item is self-contained in its own subfolder so an LLM can review
one item at a time.

Expansion strategies are registered in STRATEGIES. To add a new entity
type, implement an ExpansionStrategy subclass and register it.
"""

from __future__ import annotations

import abc
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Default S3 bucket for document archive
DEFAULT_BUCKET = "judgemind-document-archive-dev"


# ---------------------------------------------------------------------------
# Expansion strategy base class and registry
# ---------------------------------------------------------------------------


class ExpansionStrategy(abc.ABC):
    """Base class for entity-type-specific expansion strategies."""

    @abc.abstractmethod
    def expand(self, entity_id: str, item_dir: Path) -> dict[str, Any]:
        """Fetch artifacts for one entity and write them to item_dir.

        Returns a dict summarizing what was fetched.
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


def _run_db_query(
    sql: str, params: tuple[Any, ...] | None = None
) -> list[dict[str, Any]]:
    """Run a SQL query and return parsed results as a list of dicts.

    Uses psycopg directly if DATABASE_URL is set (ECS environment).
    Falls back to dev-db-query.sh locally (no parameterized queries in that mode).
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        import psycopg

        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = (
                    [desc[0] for desc in cur.description] if cur.description else []
                )
                rows = cur.fetchall()
                if not rows:
                    return []
                return [
                    {col: _serialize(val) for col, val in zip(columns, row)}
                    for row in rows
                ]

    # Local mode: use dev-db-query.sh (no parameterized queries — SQL must be
    # fully interpolated). The output contains SSM session headers/footers
    # around the JSON payload; we extract the JSON portion.
    if params is not None:
        print(
            "WARNING: Parameterized queries not supported in local mode. "
            "SQL params will be ignored — query must be pre-interpolated.",
            file=sys.stderr,
        )

    repo_root = _find_repo_root()
    result = subprocess.run(
        [str(repo_root / "scripts" / "dev-db-query.sh"), sql],
        capture_output=True,
        text=True,
        timeout=120,
    )

    # dev-db-query.sh output has SSM session preamble and trailing junk.
    # Extract the JSON array from the combined output.
    combined = result.stdout + result.stderr
    json_data = _extract_json_array(combined)
    if json_data is not None:
        return json_data

    if result.returncode != 0:
        print(f"WARNING: DB query failed: {result.stderr[:200]}", file=sys.stderr)
    else:
        print(
            f"WARNING: Could not parse DB output as JSON. First 200 chars: {combined[:200]}",
            file=sys.stderr,
        )
    return []


def _serialize(val: Any) -> Any:
    """Convert non-JSON-serializable types to strings."""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    return str(val)


def _extract_json_array(text: str) -> list[dict[str, Any]] | None:
    """Extract a JSON array from text that may contain non-JSON preamble/postamble."""
    # Find the first '[' and last ']' to extract the JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _get_bucket() -> str:
    """Get the S3 bucket name from environment or default."""
    return os.environ.get("JUDGEMIND_ARCHIVE_BUCKET") or os.environ.get(
        "S3_BUCKET", DEFAULT_BUCKET
    )


def _fetch_s3_object(s3_key: str, bucket: str, dest_path: Path) -> bool:
    """Download an S3 object to a local path. Returns True on success."""
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    try:
        s3.download_file(bucket, s3_key, str(dest_path))
        return True
    except ClientError as e:
        print(
            f"WARNING: Failed to download s3://{bucket}/{s3_key}: {e}",
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
            f"WARNING: Screenshot failed for {url_path}: {result.stderr[:200]}",
            file=sys.stderr,
        )
        return False
    return True


_REPO_ROOT: Path | None = None


def _find_repo_root() -> Path:
    """Walk up from this file to find the repo root (has CLAUDE.md)."""
    global _REPO_ROOT  # noqa: PLW0603
    if _REPO_ROOT is not None:
        return _REPO_ROOT

    for start in [Path(__file__).resolve().parent, Path.cwd()]:
        current = start
        while current != current.parent:
            if (current / "CLAUDE.md").exists():
                _REPO_ROOT = current
                return current
            current = current.parent
    msg = "Cannot find repo root"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Rulings expansion strategy
# ---------------------------------------------------------------------------


@register_strategy("rulings")
class RulingsStrategy(ExpansionStrategy):
    """Expand a ruling ID into DB record, original document, and screenshots.

    DB schema (verified):
      rulings: id, document_id, case_id, judge_id, court_id, ruling_text,
               ruling_text_html, outcome, motion_type, hearing_date, department, ...
      cases: id, case_number, case_title, case_type, court_id
      courts: id, county, court_name, court_code
      judges: id, canonical_name, court_id, department
      documents: id, s3_key, s3_bucket, format, case_id, court_id
    """

    def expand(self, entity_id: str, item_dir: Path) -> dict[str, Any]:
        result: dict[str, Any] = {"id": entity_id, "artifacts": []}

        # 1. Fetch DB record — use safe string interpolation (UUIDs only)
        if not re.match(r"^[0-9a-f-]{36}$", entity_id):
            print(f"WARNING: Invalid ruling ID format: {entity_id}", file=sys.stderr)
            return result

        query = (
            "SELECT r.id::text, r.ruling_text, r.outcome::text, r.motion_type, "
            "r.hearing_date::text, r.department, r.ruling_text_html, "
            "c.county, c.court_name, c.court_code, "
            "cs.case_number, cs.case_title, cs.case_type, cs.id::text AS case_id, "
            "j.canonical_name AS judge_name, "
            "d.s3_key, d.s3_bucket, d.format AS doc_format "
            "FROM rulings r "
            "JOIN courts c ON r.court_id = c.id "
            "JOIN cases cs ON r.case_id = cs.id "
            "LEFT JOIN judges j ON r.judge_id = j.id "
            "LEFT JOIN documents d ON r.document_id = d.id "
            f"WHERE r.id = '{entity_id}'"
        )
        records = _run_db_query(query)
        db_record_path = item_dir / "db_record.json"
        # Truncate ruling_text for the summary file (keep full text in separate file)
        for rec in records:
            full_text = rec.get("ruling_text", "")
            if full_text:
                (item_dir / "ruling_text.txt").write_text(
                    str(full_text), encoding="utf-8"
                )
                result["artifacts"].append("ruling_text.txt")
                rec["ruling_text_length"] = len(str(full_text))
                rec["ruling_text_preview"] = str(full_text)[:300]
                del rec["ruling_text"]
            html_text = rec.get("ruling_text_html")
            if html_text:
                (item_dir / "ruling_text.html").write_text(
                    str(html_text), encoding="utf-8"
                )
                result["artifacts"].append("ruling_text.html")
                del rec["ruling_text_html"]
            elif "ruling_text_html" in rec:
                del rec["ruling_text_html"]

        db_record_path.write_text(
            json.dumps(records, indent=2, default=str) + "\n", encoding="utf-8"
        )
        result["artifacts"].append("db_record.json")

        if not records:
            print(f"WARNING: No DB record for ruling {entity_id}", file=sys.stderr)
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
    """Expand an original document S3 key into doc + derived rulings.

    DB schema (verified):
      documents.s3_key links to the S3 key used in the manifest.
      rulings.document_id links rulings to their source document.
      judges.canonical_name is the judge name field.
    """

    def expand(self, entity_id: str, item_dir: Path) -> dict[str, Any]:
        result: dict[str, Any] = {"id": entity_id, "artifacts": []}
        bucket = _get_bucket()

        # 1. Fetch original document from S3
        ext = "pdf" if entity_id.endswith(".pdf") else "html"
        original_path = item_dir / f"original.{ext}"
        if _fetch_s3_object(entity_id, bucket, original_path):
            result["artifacts"].append(f"original.{ext}")

        # 2. Fetch all derived rulings from DB
        #
        # A naive ``WHERE d.s3_key = <key>`` misses rulings when the
        # queried S3 key is a content-hash-dedup *loser* (#2569): the
        # loser's document row is marked superseded with its rulings
        # deleted, while the actual rulings for those cases live on the
        # *winner*'s document (a different s3_key).  Walk the full
        # ``previous_version_id`` chain (loser → winner → terminal) so
        # any key in a multi-hop supersede chain surfaces all canonical
        # rulings.  The recursive arm joins
        # ``documents d ON d.id = td.previous_version_id`` to follow
        # each hop until no further successor exists.
        #
        # Escape single quotes in the S3 key for SQL safety.
        safe_key = entity_id.replace("'", "''")
        query = (
            "WITH RECURSIVE target_docs AS ("
            "  SELECT id, previous_version_id FROM documents WHERE s3_key = '"
            + safe_key
            + "' "
            "  UNION "
            "  SELECT d.id, d.previous_version_id "
            "  FROM documents d "
            "  JOIN target_docs td ON d.id = td.previous_version_id "
            ") "
            "SELECT r.id::text AS ruling_id, r.outcome::text, "
            "r.motion_type, r.hearing_date::text, r.department, "
            "cs.case_number, cs.case_title, "
            "j.canonical_name AS judge_name, "
            "length(r.ruling_text) AS ruling_text_length, "
            "left(r.ruling_text, 300) AS ruling_text_preview "
            "FROM rulings r "
            "JOIN target_docs td ON r.document_id = td.id "
            "JOIN cases cs ON r.case_id = cs.id "
            "LEFT JOIN judges j ON r.judge_id = j.id"
        )
        derived = _run_db_query(query)
        derived_path = item_dir / "derived_rulings.json"
        derived_path.write_text(
            json.dumps(derived, indent=2, default=str) + "\n", encoding="utf-8"
        )
        result["artifacts"].append("derived_rulings.json")
        result["derived_count"] = len(derived)

        # 3. Take screenshots for each derived ruling (skip on ECS — no browser)
        if not os.environ.get("DATABASE_URL"):
            # Local mode — can take screenshots
            if derived:
                screenshots_dir = item_dir / "ruling_screenshots"
                screenshots_dir.mkdir(exist_ok=True)
                for ruling in derived:
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


def fetch_manifest(
    manifest: dict[str, Any], output_dir: Path, *, skip_screenshots: bool = False
) -> dict[str, Any]:
    """Process a manifest and expand all entities into output_dir."""
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

    summary: dict[str, Any] = {
        "entity": entity_type,
        "county": manifest.get("county", ""),
        "total": len(ids),
        "items": [],
    }

    for i, entity_id in enumerate(ids):
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
    """Create a filesystem-safe directory name from an entity ID."""
    if len(entity_id) == 36 and entity_id.count("-") == 4:
        return entity_id
    return entity_id.replace("/", "_").replace(" ", "_")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expand a spotcheck manifest into a structured review folder",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        help="Path to manifest JSON file (from sample.py)",
    )
    parser.add_argument(
        "--manifest-json",
        help="Inline manifest JSON string (alternative to file path, for ECS)",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Output directory for expanded artifacts",
    )
    args = parser.parse_args()

    if args.manifest_json:
        manifest = json.loads(args.manifest_json)
    elif args.manifest:
        manifest_path = Path(args.manifest)
        if not manifest_path.exists():
            print(f"ERROR: Manifest not found: {manifest_path}", file=sys.stderr)
            sys.exit(1)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        print(
            "ERROR: Provide either a manifest file path or --manifest-json",
            file=sys.stderr,
        )
        sys.exit(1)
    output_dir = Path(args.output)

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
