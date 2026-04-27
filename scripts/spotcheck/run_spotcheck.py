#!/usr/bin/env python3
# venv: scraper-framework
"""One-shot bidirectional spotcheck — runs on ECS, writes results to S3.

Samples N random rulings and N random originals per county, fetches all
paired DB data, and writes a single JSON result to S3. A local caller
downloads the result and the referenced PDFs.

Usage (on ECS via ecs-run-task.sh):
    scripts/ecs-run-task.sh scripts/spotcheck/run_spotcheck.py -- --n 10

Usage (single county):
    scripts/ecs-run-task.sh scripts/spotcheck/run_spotcheck.py -- --n 10 --county Orange

The result JSON is written to:
    s3://<archive-bucket>/spotcheck/<timestamp>.json

The caller can download it with:
    aws s3 cp s3://<bucket>/spotcheck/<timestamp>.json tmp/spotcheck/data.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

import boto3
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
BUCKET = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev")

ALL_COUNTIES = [
    "Los Angeles", "Orange", "Riverside", "San Bernardino",
    "Santa Clara", "Ventura", "Contra Costa", "Fresno",
    "San Francisco", "San Diego",
]


def sample_rulings(cur: psycopg.Cursor, county: str, n: int) -> list[dict]:
    """Sample N random rulings for a county with full context."""
    cur.execute("""
        SELECT
            r.id::text AS ruling_id,
            r.outcome::text,
            r.motion_type,
            r.hearing_date::text,
            r.department,
            cs.case_number,
            cs.case_title,
            cs.case_type,
            j.canonical_name AS judge_name,
            d.s3_key,
            d.s3_bucket,
            d.format AS doc_format,
            c.county,
            length(r.ruling_text) AS ruling_text_length,
            left(r.ruling_text, 500) AS ruling_text_preview
        FROM rulings r
        JOIN courts c ON r.court_id = c.id
        JOIN cases cs ON r.case_id = cs.id
        LEFT JOIN judges j ON r.judge_id = j.id
        LEFT JOIN documents d ON r.document_id = d.id
        WHERE c.county = %s
        ORDER BY random()
        LIMIT %s
    """, (county, n))
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def sample_originals(cur: psycopg.Cursor, county: str, n: int) -> list[dict]:
    """Sample N random original documents and their derived rulings."""
    # Sample from documents table (not S3) to avoid orphaned keys
    cur.execute("""
        WITH sampled AS (
            SELECT DISTINCT ON (d.s3_key)
                d.s3_key, d.s3_bucket, d.id::text AS doc_id
            FROM documents d
            JOIN courts c ON d.court_id = c.id
            WHERE c.county = %s AND d.s3_key IS NOT NULL
            ORDER BY d.s3_key, random()
        )
        SELECT s3_key, s3_bucket, doc_id FROM sampled
        ORDER BY random() LIMIT %s
    """, (county, n))
    sampled_docs = cur.fetchall()

    originals = []
    for s3_key, s3_bucket, doc_id in sampled_docs:
        # A naive ``WHERE d.s3_key = <key>`` misses rulings when the queried
        # S3 key is a content-hash-dedup *loser* (#2569): the loser's document
        # row is marked superseded with its rulings deleted, while the actual
        # rulings for those cases live on the *winner*'s document (a different
        # s3_key).  Follow ``documents.previous_version_id`` so either the
        # winner or the loser key surfaces the canonical rulings.  Mirrors the
        # CTE pattern in ``scripts/spotcheck/fetch.py`` ``OriginalsStrategy``.
        cur.execute("""
            WITH target_docs AS (
                SELECT id FROM documents WHERE s3_key = %s
                UNION
                SELECT previous_version_id AS id FROM documents
                WHERE s3_key = %s AND previous_version_id IS NOT NULL
            )
            SELECT r.id::text AS ruling_id, r.outcome::text,
                   r.motion_type, r.hearing_date::text, r.department,
                   cs.case_number, cs.case_title,
                   j.canonical_name AS judge_name,
                   length(r.ruling_text) AS ruling_text_length,
                   left(r.ruling_text, 300) AS ruling_text_preview
            FROM rulings r
            JOIN target_docs td ON r.document_id = td.id
            JOIN cases cs ON r.case_id = cs.id
            LEFT JOIN judges j ON r.judge_id = j.id
            ORDER BY r.id
        """, (s3_key, s3_key))
        cols = [desc[0] for desc in cur.description]
        derived = [dict(zip(cols, row)) for row in cur.fetchall()]

        case_titles = list({r["case_title"] for r in derived if r.get("case_title")})

        originals.append({
            "s3_key": s3_key,
            "s3_bucket": s3_bucket,
            "derived_count": len(derived),
            "all_same_case": len(case_titles) == 1 and len(derived) > 1,
            "distinct_case_titles": len(case_titles),
            "derived_rulings": derived,
        })

    return originals


def get_county_stats(cur: psycopg.Cursor) -> dict:
    """Get per-county ruling counts and latest timestamps."""
    cur.execute("""
        SELECT c.county, COUNT(r.id) AS ruling_count,
               COUNT(DISTINCT d.s3_key) AS distinct_docs,
               MAX(r.created_at)::text AS latest
        FROM rulings r
        JOIN courts c ON r.court_id = c.id
        LEFT JOIN documents d ON r.document_id = d.id
        GROUP BY c.county ORDER BY c.county
    """)
    stats = {}
    for row in cur.fetchall():
        stats[row[0]] = {
            "ruling_count": row[1],
            "distinct_docs": row[2],
            "latest": row[3],
        }
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bidirectional spotcheck")
    parser.add_argument("--n", type=int, default=10, help="Samples per county per direction")
    parser.add_argument("--county", help="Single county to check (default: all)")
    args = parser.parse_args()

    counties = ALL_COUNTIES
    if args.county:
        counties = [c for c in ALL_COUNTIES if c.lower() == args.county.lower()]
        if not counties:
            counties = [args.county]

    result = {
        "timestamp": datetime.now(UTC).isoformat(),
        "n_per_county": args.n,
        "counties_requested": counties,
        "county_stats": {},
        "rulings_by_county": {},
        "originals_by_county": {},
    }

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            result["county_stats"] = get_county_stats(cur)

            for county in counties:
                stats = result["county_stats"].get(county, {})
                if not stats or stats.get("ruling_count", 0) == 0:
                    print(f"Skipping {county} — no rulings in DB", file=sys.stderr)
                    result["rulings_by_county"][county] = []
                    result["originals_by_county"][county] = []
                    continue

                print(f"Sampling {county} ({stats['ruling_count']} rulings)...",
                      file=sys.stderr)

                result["rulings_by_county"][county] = sample_rulings(
                    cur, county, args.n
                )
                result["originals_by_county"][county] = sample_originals(
                    cur, county, args.n
                )

    # Write result to S3
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"spotcheck/{ts}.json"
    result_json = json.dumps(result, indent=2, default=str)

    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=BUCKET,
        Key=s3_key,
        Body=result_json.encode("utf-8"),
        ContentType="application/json",
    )

    print(f"\nResult written to s3://{BUCKET}/{s3_key}", file=sys.stderr)
    print(f"Download with: aws s3 cp s3://{BUCKET}/{s3_key} tmp/spotcheck/data.json",
          file=sys.stderr)

    # Also print a compact summary to stdout (this shows in CloudWatch logs)
    summary = {
        "s3_result": f"s3://{BUCKET}/{s3_key}",
        "counties": {},
    }
    for county in counties:
        rulings = result["rulings_by_county"].get(county, [])
        originals = result["originals_by_county"].get(county, [])
        same_case_count = sum(1 for o in originals if o.get("all_same_case"))
        zero_derived = sum(1 for o in originals if o.get("derived_count", 0) == 0)
        null_judges = sum(1 for r in rulings if not r.get("judge_name"))
        unknown_cases = sum(1 for r in rulings
                          if (r.get("case_number") or "").startswith("UNKNOWN"))
        null_departments = sum(1 for r in rulings if not r.get("department"))
        empty_ruling_text = sum(
            1 for r in rulings if (r.get("ruling_text_length") or 0) == 0
        )
        long_case_titles = sum(
            1 for r in rulings if len(r.get("case_title") or "") > 100
        )
        # Also check originals' derived rulings for empty text and long titles
        orig_empty_text = sum(
            1 for o in originals
            for dr in o.get("derived_rulings", [])
            if (dr.get("ruling_text_length") or 0) == 0
        )
        orig_long_titles = sum(
            1 for o in originals
            for dr in o.get("derived_rulings", [])
            if len(dr.get("case_title") or "") > 100
        )
        summary["counties"][county] = {
            "rulings_sampled": len(rulings),
            "originals_sampled": len(originals),
            "null_judges": null_judges,
            "null_departments": null_departments,
            "unknown_case_numbers": unknown_cases,
            "empty_ruling_text": empty_ruling_text,
            "long_case_titles": long_case_titles,
            "originals_all_same_case": same_case_count,
            "originals_zero_derived": zero_derived,
            "originals_empty_text": orig_empty_text,
            "originals_long_titles": orig_long_titles,
        }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
