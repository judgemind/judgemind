#!/usr/bin/env python3
"""Audit field completeness across all counties.

Queries the database and produces a per-county report showing what
percentage of documents have each required field populated.

Required fields (per architecture spec):
  judge name, motion type, case title, hearing date, outcome, parties

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/audit_field_completeness.py

Options:
    --json          Machine-readable JSON output.
    --verbose       List specific document IDs with missing fields.
    --county NAME   Audit only the specified county.

Exit code: 0 if all fields are 100%, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json as json_mod
import logging
import os
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

AUDIT_QUERY = """
    SELECT
        ct.county,
        COUNT(d.id) AS total_docs,
        COUNT(r.id) AS has_ruling,
        COUNT(r.judge_id) AS has_judge,
        COUNT(r.motion_type) AS has_motion_type,
        COUNT(r.outcome) AS has_outcome,
        COUNT(CASE WHEN c.case_title IS NOT NULL THEN 1 END) AS has_title,
        COUNT(CASE WHEN c.case_number NOT LIKE 'UNKNOWN-%%' THEN 1 END) AS has_case_number,
        COUNT(CASE WHEN EXISTS (
            SELECT 1 FROM case_parties cp WHERE cp.case_id = c.id
        ) THEN 1 END) AS has_parties,
        COUNT(d.hearing_date) AS has_hearing_date
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN rulings r ON r.document_id = d.id
    LEFT JOIN cases c ON c.id = d.case_id
    WHERE d.status = 'active'
    {county_filter}
    GROUP BY ct.county ORDER BY ct.county
"""

VERBOSE_QUERY = """
    SELECT
        ct.county,
        d.id AS document_id,
        c.case_number,
        CASE WHEN r.id IS NULL THEN 'ruling' ELSE NULL END AS missing_ruling,
        CASE WHEN r.judge_id IS NULL AND r.id IS NOT NULL THEN 'judge' ELSE NULL END AS missing_judge,
        CASE WHEN r.motion_type IS NULL AND r.id IS NOT NULL THEN 'motion_type' ELSE NULL END AS missing_motion_type,
        CASE WHEN r.outcome IS NULL AND r.id IS NOT NULL THEN 'outcome' ELSE NULL END AS missing_outcome,
        CASE WHEN c.case_title IS NULL THEN 'case_title' ELSE NULL END AS missing_title,
        CASE WHEN c.case_number LIKE 'UNKNOWN-%%' THEN 'case_number' ELSE NULL END AS missing_case_number,
        CASE WHEN NOT EXISTS (
            SELECT 1 FROM case_parties cp WHERE cp.case_id = c.id
        ) THEN 'parties' ELSE NULL END AS missing_parties,
        CASE WHEN d.hearing_date IS NULL THEN 'hearing_date' ELSE NULL END AS missing_hearing_date
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN rulings r ON r.document_id = d.id
    LEFT JOIN cases c ON c.id = d.case_id
    WHERE d.status = 'active'
    {county_filter}
    ORDER BY ct.county, d.id
"""


def _pct(count: int, total: int) -> str:
    if total == 0:
        return "N/A"
    return f"{count / total * 100:.1f}%"


def run_audit(
    dsn: str,
    *,
    county: str | None = None,
    output_json: bool = False,
    verbose: bool = False,
) -> bool:
    """Run the audit and print results. Returns True if 100% complete."""
    county_filter = ""
    params: tuple[str, ...] = ()
    if county:
        county_filter = "AND ct.county = %s"
        params = (county,)

    all_complete = True
    results: list[dict] = []

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(AUDIT_QUERY.format(county_filter=county_filter), params)
            rows = cur.fetchall()

        for row in rows:
            (
                county_name,
                total,
                has_ruling,
                has_judge,
                has_motion_type,
                has_outcome,
                has_title,
                has_case_number,
                has_parties,
                has_hearing_date,
            ) = row

            county_result = {
                "county": county_name,
                "total_documents": total,
                "fields": {
                    "ruling": {
                        "count": has_ruling,
                        "total": total,
                        "pct": round(has_ruling / total * 100, 1) if total else 0,
                    },
                    "judge": {
                        "count": has_judge,
                        "total": total,
                        "pct": round(has_judge / total * 100, 1) if total else 0,
                    },
                    "motion_type": {
                        "count": has_motion_type,
                        "total": total,
                        "pct": round(has_motion_type / total * 100, 1) if total else 0,
                    },
                    "outcome": {
                        "count": has_outcome,
                        "total": total,
                        "pct": round(has_outcome / total * 100, 1) if total else 0,
                    },
                    "case_title": {
                        "count": has_title,
                        "total": total,
                        "pct": round(has_title / total * 100, 1) if total else 0,
                    },
                    "case_number": {
                        "count": has_case_number,
                        "total": total,
                        "pct": round(has_case_number / total * 100, 1) if total else 0,
                    },
                    "parties": {
                        "count": has_parties,
                        "total": total,
                        "pct": round(has_parties / total * 100, 1) if total else 0,
                    },
                    "hearing_date": {
                        "count": has_hearing_date,
                        "total": total,
                        "pct": round(has_hearing_date / total * 100, 1) if total else 0,
                    },
                },
            }

            # Check if any field is below 100%
            for field_info in county_result["fields"].values():
                if field_info["count"] < field_info["total"]:
                    all_complete = False

            results.append(county_result)

        # Verbose: list documents with missing fields
        verbose_data: list[dict] = []
        if verbose:
            with conn.cursor() as cur:
                cur.execute(VERBOSE_QUERY.format(county_filter=county_filter), params)
                vrows = cur.fetchall()

            for vrow in vrows:
                missing = [
                    f
                    for f in vrow[3:]  # columns after case_number
                    if f is not None
                ]
                if missing:
                    verbose_data.append(
                        {
                            "county": vrow[0],
                            "document_id": str(vrow[1]),
                            "case_number": vrow[2],
                            "missing_fields": missing,
                        }
                    )

    if output_json:
        output = {"complete": all_complete, "counties": results}
        if verbose:
            output["documents_with_gaps"] = verbose_data
        print(json_mod.dumps(output, indent=2))
    else:
        _print_table(results)
        if verbose and verbose_data:
            print(
                f"\n--- Documents with missing fields ({len(verbose_data)} total) ---"
            )
            for item in verbose_data[:50]:  # cap output
                print(
                    f"  {item['county']:20s} {item['document_id']}  "
                    f"case={item['case_number'] or 'N/A':20s}  "
                    f"missing: {', '.join(item['missing_fields'])}"
                )
            if len(verbose_data) > 50:
                print(f"  ... and {len(verbose_data) - 50} more")
        if all_complete:
            print("\nAll fields 100% complete!")
        else:
            print("\nGaps remain — see above.")

    return all_complete


def _print_table(results: list[dict]) -> None:
    """Print a formatted text table of audit results."""
    fields = [
        "ruling",
        "judge",
        "motion_type",
        "outcome",
        "case_title",
        "case_number",
        "parties",
        "hearing_date",
    ]
    header = f"{'County':20s} {'Total':>6s}"
    for f in fields:
        header += f" {f:>14s}"
    print(header)
    print("-" * len(header))

    for r in results:
        line = f"{r['county']:20s} {r['total_documents']:6d}"
        for f in fields:
            fi = r["fields"][f]
            line += f" {fi['count']:5d}/{fi['total']:<5d} {fi['pct']:>5.1f}%"
        # Simplify: just show pct
        line = f"{r['county']:20s} {r['total_documents']:6d}"
        for f in fields:
            fi = r["fields"][f]
            pct = fi["pct"]
            marker = " " if pct == 100.0 else "*"
            line += f" {pct:>6.1f}%{marker}"
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit field completeness across counties.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="List documents with gaps."
    )
    parser.add_argument(
        "--county", type=str, default=None, help="Audit only this county."
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    all_complete = run_audit(
        dsn,
        county=args.county,
        output_json=args.json,
        verbose=args.verbose,
    )
    sys.exit(0 if all_complete else 1)


if __name__ == "__main__":
    main()
