#!/usr/bin/env python3
"""Retroactive audit for document-level deterministic validation rules.

PR #2396 shipped ``check_no_cross_case_ruling_text`` as a document-level
deterministic validation rule that runs during ingestion.  It only flags
rulings at *ingest time* — existing rows in ``derived.rulings`` that were
ingested before the rule shipped are not flagged, and a full reingest is
expensive (LLM calls, S3 reads).

This script re-runs document-level deterministic rules against the
current contents of ``derived.rulings`` and optionally writes flag rows
to ``telemetry.validation_results`` so the per-county follow-up issues
(#2562, #2563, #2564, #2565, #2566) can measure baseline contamination
quantitatively.

The script groups rulings by ``(court_id, s3_key)`` because multi-case
calendar PDFs are represented as N split-child documents all sharing the
same raw S3 object (see ``ingestion.split_ids``).  Only groups with
2+ rulings are audited — singleton groups cannot produce cross-case
flags because there are no siblings to cross-reference.

Usage (via ECS, for dev DB access):

    scripts/ecs-run-task.sh --script scripts/audit_document_validation_rules.py -- \\
        --county "San Bernardino" --rule no_cross_case_ruling_text --write

Local / dry-run (connects to whatever ``DATABASE_URL`` points at):

    scripts/run-py.sh scripts/audit_document_validation_rules.py --json
    scripts/run-py.sh scripts/audit_document_validation_rules.py --county "Santa Clara"

CLI flags:
  --county NAME   Restrict to a single county (matches ``derived.courts.county``).
  --rule NAME     Restrict to a single document-level rule.  Currently the only
                  supported value is ``no_cross_case_ruling_text``.
  --dry-run       Do not write to the DB.  This is the DEFAULT.
  --write         Insert one ``telemetry.validation_results`` row per flag.
                  Mutually exclusive with ``--dry-run``.
  --json          Emit a machine-readable JSON report on stdout.
  --limit N       Scan at most N rulings (useful for smoke tests on dev).

Exit code: ``0`` when zero flags are found, ``1`` otherwise — same convention
as the other ``scripts/audit_*.py`` helpers.

See issue #2567.
"""

# venv: scraper-framework
# one-off: true

from __future__ import annotations

import argparse
import datetime as _dt
import json as json_mod
import logging
import os
import sys
from collections import defaultdict
from typing import Any

import psycopg

from validation.deterministic import (
    check_no_cross_case_ruling_text,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported rules
# ---------------------------------------------------------------------------

# Mapping of supported document-level rule names to the callable that
# evaluates them on a single ruling given its sibling case numbers.
# Each callable must accept ``(ruling_text, own_case_number, other_case_numbers)``
# and return a ``DeterministicRuleResult``.
_DOCUMENT_LEVEL_RULES: dict[str, Any] = {
    "no_cross_case_ruling_text": check_no_cross_case_ruling_text,
}

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Fetch the fields needed to re-run document-level rules.  We use
# ``d.s3_key`` (plus ``d.court_id``) as the sibling grouping key because
# split-child documents share the same raw S3 object.  ``captured_at`` and
# ``hearing_date`` are fetched for consistency with per-ruling rules even
# though no document-level rule currently uses them.
_AUDIT_QUERY = """
    SELECT
        r.id::text          AS ruling_id,
        d.id::text          AS document_id,
        d.court_id::text    AS court_id,
        d.s3_key            AS s3_key,
        c.case_number       AS case_number,
        c.case_title        AS case_title,
        r.ruling_text       AS ruling_text,
        r.hearing_date      AS hearing_date,
        d.captured_at       AS captured_at,
        ct.county           AS county
    FROM derived.rulings r
    JOIN derived.documents d ON d.id = r.document_id
    JOIN derived.courts ct   ON ct.id = d.court_id
    LEFT JOIN derived.cases c ON c.id = r.case_id
    WHERE d.status = 'active'
      {county_clause}
    ORDER BY d.court_id, d.s3_key, r.id
    {limit_clause}
"""


def _build_query(county: str | None, limit: int | None) -> tuple[str, list[Any]]:
    """Build the audit query and parameters, substituting optional clauses."""
    params: list[Any] = []
    county_clause = ""
    if county:
        county_clause = "AND ct.county = %s"
        params.append(county)
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT %s"
        params.append(limit)
    return (
        _AUDIT_QUERY.format(
            county_clause=county_clause,
            limit_clause=limit_clause,
        ),
        params,
    )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def group_rulings_by_source(rulings: list[dict]) -> list[dict]:
    """Group rulings by ``(court_id, s3_key)`` and drop singletons.

    Parameters
    ----------
    rulings:
        Rows from ``_AUDIT_QUERY`` (or equivalent) as dicts.

    Returns
    -------
    list[dict]
        One group per multi-ruling source document, each shaped as::

            {"court_id": str, "s3_key": str, "rulings": list[dict]}

        Singleton groups are excluded — cross-case rules need at least two
        siblings.
    """
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rulings:
        court_id = r.get("court_id")
        s3_key = r.get("s3_key")
        if not court_id or not s3_key:
            continue
        buckets[(str(court_id), str(s3_key))].append(r)

    groups: list[dict] = []
    for (court_id, s3_key), members in buckets.items():
        if len(members) < 2:
            continue
        groups.append(
            {
                "court_id": court_id,
                "s3_key": s3_key,
                "rulings": members,
            }
        )
    return groups


# ---------------------------------------------------------------------------
# Auditing
# ---------------------------------------------------------------------------


def _resolve_rules(rule_filter: str | None) -> dict[str, Any]:
    """Resolve the rule_filter argument into a dict of rule_name → checker."""
    if rule_filter is None:
        return dict(_DOCUMENT_LEVEL_RULES)
    if rule_filter not in _DOCUMENT_LEVEL_RULES:
        raise ValueError(
            f"Unknown rule '{rule_filter}'. Supported: {sorted(_DOCUMENT_LEVEL_RULES)}"
        )
    return {rule_filter: _DOCUMENT_LEVEL_RULES[rule_filter]}


def audit_group(group: dict, *, rule_filter: str | None) -> list[dict]:
    """Run document-level rules over a single sibling group and return flags.

    For each ruling in the group, evaluate each selected document-level rule
    using sibling context (the other rulings' case numbers).  Each rule that
    returns ``flag`` or ``fail`` produces one entry in the returned list.

    Parameters
    ----------
    group:
        A group dict produced by :func:`group_rulings_by_source`.
    rule_filter:
        Optional name of a single rule to run.  ``None`` runs all supported
        document-level rules.

    Returns
    -------
    list[dict]
        Zero or more flag entries, each shaped as::

            {
                "ruling_id": str,
                "document_id": str,
                "rule": str,
                "reason": str,
                "result": "flag" | "fail",
                "county": str,
            }
    """
    rules = _resolve_rules(rule_filter)
    flags: list[dict] = []
    members = group["rulings"]

    for member in members:
        own_case_number = member.get("case_number")
        # Sibling case numbers = all non-empty case numbers from other rulings.
        sibling_nums: list[str] = [
            str(other.get("case_number"))
            for other in members
            if other is not member and other.get("case_number")
        ]
        if not sibling_nums:
            continue
        ruling_text = member.get("ruling_text")
        # Skip rulings with no text at all — nothing to scan.
        if not ruling_text:
            continue

        for rule_name, checker in rules.items():
            if rule_name == "no_cross_case_ruling_text":
                # Need an own_case_number to run this rule meaningfully — a
                # missing own number means we can't exclude own-case matches
                # from siblings.  Skip rather than emit noise.
                if not own_case_number:
                    continue
                result = checker(
                    ruling_text=ruling_text,
                    own_case_number=own_case_number,
                    other_case_numbers=sibling_nums,
                )
            else:  # pragma: no cover - defensive for future rules
                continue

            if result.result not in ("flag", "fail"):
                continue

            flags.append(
                {
                    "ruling_id": str(member["ruling_id"]),
                    "document_id": str(member["document_id"]),
                    "rule": rule_name,
                    "reason": result.reason or "",
                    "result": result.result,
                    "county": member.get("county"),
                }
            )

    return flags


# ---------------------------------------------------------------------------
# DB writeback
# ---------------------------------------------------------------------------


def write_flags_to_db(conn: psycopg.Connection, flags: list[dict]) -> int:
    """Insert one ``telemetry.validation_results`` row per flag.

    The table schema (see ``packages/api/migrations/13_validation-results.sql``)
    does not include a ``rule`` column — the ``result`` column is constrained
    to one of ``pass|flag|fail|error`` and the ``model`` column identifies the
    originating validator.  To preserve per-rule provenance we:

    * Set ``model`` to ``"deterministic"`` (matching the ingestion worker).
    * Prefix ``reason`` with ``[rule_name]`` so the per-rule query on the
      issue ticket (``WHERE reason LIKE '[no_cross_case_ruling_text]%%'``)
      still works even without a dedicated column.

    Parameters
    ----------
    conn:
        Active psycopg connection.  Caller manages commit/rollback.
    flags:
        Flag entries returned by :func:`audit_group`.

    Returns
    -------
    int
        Number of rows inserted.
    """
    if not flags:
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for f in flags:
            reason_with_rule = f"[{f['rule']}] {f['reason']}".strip()
            cur.execute(
                """
                INSERT INTO validation_results
                    (document_id, ruling_id, result, reason, model,
                     input_tokens, output_tokens, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f["document_id"],
                    f["ruling_id"],
                    f["result"],
                    reason_with_rule,
                    "deterministic",
                    0,
                    0,
                    0,
                ),
            )
            inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_report(
    flags: list[dict],
    *,
    groups_scanned: int,
    rulings_scanned: int,
) -> dict:
    """Build a JSON-serialisable audit report."""
    per_county_counts: dict[str, int] = defaultdict(int)
    for f in flags:
        per_county_counts[f.get("county") or "<unknown>"] += 1
    per_county = [
        {"county": county, "flag_count": count}
        for county, count in sorted(per_county_counts.items())
    ]
    per_rule_counts: dict[str, int] = defaultdict(int)
    for f in flags:
        per_rule_counts[f["rule"]] += 1
    per_rule = [
        {"rule": rule, "flag_count": count}
        for rule, count in sorted(per_rule_counts.items())
    ]
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "groups_scanned": groups_scanned,
        "rulings_scanned": rulings_scanned,
        "total_flags": len(flags),
        "per_county": per_county,
        "per_rule": per_rule,
        "flags": flags,
    }


def _print_human_report(report: dict) -> None:
    """Print a terse human-readable summary to stdout."""
    print("Document-level validation audit")
    print("=" * 50)
    print(f"Rulings scanned: {report['rulings_scanned']}")
    print(f"Sibling groups:  {report['groups_scanned']}")
    print(f"Total flags:     {report['total_flags']}")
    print()
    print("Flags per county:")
    if not report["per_county"]:
        print("  (none)")
    else:
        for entry in report["per_county"]:
            print(f"  {entry['county']:<25s} {entry['flag_count']:>6d}")
    print()
    print("Flags per rule:")
    if not report["per_rule"]:
        print("  (none)")
    else:
        for entry in report["per_rule"]:
            print(f"  {entry['rule']:<40s} {entry['flag_count']:>6d}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_audit(
    dsn: str,
    *,
    county: str | None,
    rule_filter: str | None,
    write: bool,
    limit: int | None,
) -> dict:
    """Run the full audit against ``dsn`` and return the report dict.

    Opens a single psycopg connection, fetches all candidate rulings, groups
    them, runs the selected document-level rules, optionally writes flags to
    ``validation_results``, and returns the report.
    """
    query, params = _build_query(county=county, limit=limit)

    rulings: list[dict] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur:
                rulings.append(dict(zip(cols, row, strict=True)))

        groups = group_rulings_by_source(rulings)
        all_flags: list[dict] = []
        for group in groups:
            all_flags.extend(audit_group(group, rule_filter=rule_filter))

        if write and all_flags:
            inserted = write_flags_to_db(conn, all_flags)
            conn.commit()
            logger.info("Wrote %d validation_results rows", inserted)

    return build_report(
        all_flags,
        groups_scanned=len(groups),
        rulings_scanned=len(rulings),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Retroactively run document-level deterministic validation "
            "rules against derived.rulings."
        )
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Restrict audit to a single county.",
    )
    parser.add_argument(
        "--rule",
        type=str,
        default=None,
        choices=sorted(_DOCUMENT_LEVEL_RULES),
        help="Restrict audit to a single document-level rule.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write to the DB (default).",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Insert one telemetry.validation_results row per flag.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report on stdout.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Scan at most N rulings (useful for smoke tests).",
    )
    args = parser.parse_args(argv)

    # Default to dry-run when neither mode flag is set.
    if not args.write:
        args.dry_run = True

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        return 1

    report = run_audit(
        dsn,
        county=args.county,
        rule_filter=args.rule,
        write=args.write,
        limit=args.limit,
    )

    if args.json:
        print(json_mod.dumps(report, indent=2, default=str))
    else:
        _print_human_report(report)

    return 0 if report["total_flags"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
