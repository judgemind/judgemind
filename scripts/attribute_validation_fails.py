#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Report validation-gate fails per county / rule / raw S3 key — #4728.

A validation ``fail`` never writes a ``derived.documents`` row, and split
children carry uuid5 ids, so a ``telemetry.validation_results`` row could
not be traced back to its raw capture.  Since #4706 (migration 66, worker
change PR #4773, first attributed row 2026-09-26T00:18Z) the worker writes
``county``, ``scraper_id`` and ``s3_key`` on every row; split children carry
the parent event's ``s3_key``.  This script groups those rows over a time
window.

Read-only: the session is opened ``read_only`` so any write fails.
``telemetry.*`` is accumulated state and is not rebuildable from S3.

Rows written before #4706 have NULL attribution.  ``--legacy`` attributes
them best-effort at read time through ``document_id -> derived.documents``
(only rows whose ``document_id`` still has a documents row, e.g. a document
a later re-ingest wrote; a failed split child never got one).  Nothing is
backfilled -- see
``docs/agent/observability.md`` for why.

The rule a row tripped is not stored, only the ``reason`` text (several
deterministic reasons joined by ``"; "``).  ``RULE_MARKERS`` maps each
deterministic rule in ``validation/deterministic.py`` to a fixed fragment of
its reason; a row can count under more than one rule.  LLM-gate rows
(``model`` is not ``deterministic``) report as ``llm``.

Usage (ECS, against dev):
    scripts/ecs-run-task.sh scripts/attribute_validation_fails.py
    scripts/ecs-run-task.sh scripts/attribute_validation_fails.py -- \\
        --county "Santa Clara" --since 2026-10-01T00:00:00Z --top 50
    scripts/ecs-run-task.sh scripts/attribute_validation_fails.py -- \\
        --result flag --rule case_number_not_unknown --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any

# First attributed row on dev was written 2026-09-26T00:18Z, right after the
# #4773 deploy.  Earlier rows have NULL county / scraper_id / s3_key.
DEFAULT_SINCE = "2026-09-26T00:00:00Z"

# (rule, fixed fragment of its reason) — keep in sync with the ``reason=``
# strings in packages/scraper-framework/src/validation/deterministic.py.
# test_attribute_validation_fails.py fails if a rule's reason stops matching.
RULE_MARKERS: tuple[tuple[str, str], ...] = (
    ("no_html_in_ruling_text", "raw HTML detected"),
    ("hearing_date_in_range", "hearing_date ("),
    ("no_multiple_adversarial_patterns", "likely multi-case contamination"),
    ("ruling_text_not_empty", "ruling_text is null or empty"),
    ("case_number_not_unknown", "extraction couldn't find case number"),
    ("unknown_and_null_title_fail", "bad-split fragment"),
    ("no_duplicate_ruling_text", "duplicate ruling_text lengths"),
    ("no_cross_case_ruling_text", "other case number(s) from the same document"),
    ("ruling_text_case_number_marker", "'Superior Court Case No.' marker"),
    ("ruling_text_reasonable_length", "(truncation sentinel)"),
)

# One row per (county, scraper_id, s3_key, model, reason, attribution).
# Attribution: 'row' = written on the telemetry row (#4706); 'document' =
# recovered from derived.documents (--legacy only); 'none' = unattributed.
_QUERY = """
SELECT {county} AS county,
       {scraper_id} AS scraper_id,
       {s3_key} AS s3_key,
       v.model,
       v.reason,
       {attribution} AS attribution,
       count(*) AS n
FROM telemetry.validation_results v
{joins}
WHERE v.result = %(result)s
  AND v.created_at >= %(since)s::timestamptz
  AND (%(until)s::timestamptz IS NULL OR v.created_at < %(until)s::timestamptz)
GROUP BY 1, 2, 3, 4, 5, 6
"""

_PLAIN = {
    "county": "v.county",
    "scraper_id": "v.scraper_id",
    "s3_key": "v.s3_key",
    "attribution": "CASE WHEN v.s3_key IS NOT NULL THEN 'row' ELSE 'none' END",
    "joins": "",
}

_LEGACY = {
    "county": "COALESCE(v.county, c.county)",
    "scraper_id": "COALESCE(v.scraper_id, d.scraper_id)",
    "s3_key": "COALESCE(v.s3_key, d.s3_key)",
    "attribution": (
        "CASE WHEN v.s3_key IS NOT NULL THEN 'row' "
        "WHEN d.s3_key IS NOT NULL THEN 'document' ELSE 'none' END"
    ),
    "joins": (
        "LEFT JOIN derived.documents d\n"
        "  ON v.s3_key IS NULL AND d.id = v.document_id\n"
        "LEFT JOIN derived.courts c ON c.id = d.court_id"
    ),
}


def build_query(legacy: bool) -> str:
    return _QUERY.format(**(_LEGACY if legacy else _PLAIN))


def rules_for(reason: str | None, model: str | None) -> list[str]:
    """Return the rules a reason names, ``llm`` for LLM-gate rows, else ``other``."""
    if model is not None and model != "deterministic":
        return ["llm"]
    text = reason or ""
    hits = [rule for rule, marker in RULE_MARKERS if marker in text]
    return hits or ["other"]


@dataclass
class Row:
    county: str | None
    scraper_id: str | None
    s3_key: str | None
    model: str | None
    reason: str | None
    attribution: str
    n: int


def fetch(conn: Any, args: argparse.Namespace) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(
            build_query(args.legacy),
            {"result": args.result, "since": args.since, "until": args.until},
        )
        return [Row(*r) for r in cur.fetchall()]


def aggregate(
    rows: list[Row],
    *,
    county: str | None = None,
    scraper_id: str | None = None,
    rule: str | None = None,
    top: int = 25,
) -> dict[str, Any]:
    """Filter and group fetched rows.  Filters apply after attribution, so
    ``--legacy`` rows recovered from derived.documents are filtered too."""
    by_attr: Counter[str] = Counter()
    by_county: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    by_key: Counter[str] = Counter()
    key_rules: dict[str, Counter[str]] = {}
    total = 0
    for r in rows:
        rules = rules_for(r.reason, r.model)
        if county is not None and r.county != county:
            continue
        if scraper_id is not None and r.scraper_id != scraper_id:
            continue
        if rule is not None and rule not in rules:
            continue
        total += r.n
        by_attr[r.attribution] += r.n
        by_county[f"{r.county or '(none)'} / {r.scraper_id or '(none)'}"] += r.n
        for name in rules:
            by_rule[name] += r.n
        if r.s3_key is not None:
            by_key[r.s3_key] += r.n
            key_rules.setdefault(r.s3_key, Counter()).update(dict.fromkeys(rules, r.n))
    return {
        "total": total,
        "by_attribution": dict(by_attr.most_common()),
        "by_county": dict(by_county.most_common()),
        "by_rule": dict(by_rule.most_common()),
        "distinct_s3_keys": len(by_key),
        "top_s3_keys": [
            {"s3_key": k, "n": n, "rules": sorted(key_rules[k])}
            for k, n in by_key.most_common(top)
        ],
    }


def render(report: dict[str, Any], args: argparse.Namespace) -> str:
    window = f"{args.since} .. {args.until or 'now'}"
    legacy = " (legacy attribution on)" if args.legacy else ""
    lines = [
        f"validation_results result={args.result} window={window}{legacy}",
        f"total rows: {report['total']}",
        "",
        "by attribution:",
    ]
    lines += [f"  {n:>7}  {k}" for k, n in report["by_attribution"].items()]
    lines += ["", "by county / scraper_id:"]
    lines += [f"  {n:>7}  {k}" for k, n in report["by_county"].items()]
    lines += ["", "by rule (a row can match several rules):"]
    lines += [f"  {n:>7}  {k}" for k, n in report["by_rule"].items()]
    lines += [
        "",
        f"top {len(report['top_s3_keys'])} of {report['distinct_s3_keys']} s3 keys:",
    ]
    lines += [
        f"  {e['n']:>7}  {e['s3_key']}  [{', '.join(e['rules'])}]"
        for e in report["top_s3_keys"]
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=f"inclusive lower bound on created_at (default {DEFAULT_SINCE}, "
        "the #4706 deploy)",
    )
    p.add_argument("--until", default=None, help="exclusive upper bound on created_at")
    p.add_argument("--county", default=None, help="exact county, e.g. 'Santa Clara'")
    p.add_argument("--scraper-id", default=None, help="exact scraper_id")
    p.add_argument(
        "--rule",
        default=None,
        choices=[r for r, _ in RULE_MARKERS] + ["llm", "other"],
        help="only rows whose reason matches this rule",
    )
    p.add_argument(
        "--result",
        default="fail",
        choices=["fail", "flag", "error"],
        help="validation outcome to report (default fail)",
    )
    p.add_argument("--top", type=int, default=25, help="how many s3 keys to list")
    p.add_argument(
        "--legacy",
        action="store_true",
        help="attribute pre-#4706 NULL rows via derived.documents where it exists",
    )
    p.add_argument("--json", action="store_true", help="print one JSON object")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        rows = fetch(conn, args)
    report = aggregate(
        rows,
        county=args.county,
        scraper_id=args.scraper_id,
        rule=args.rule,
        top=args.top,
    )
    if args.json:
        print(json.dumps({"args": vars(args), **report}, sort_keys=True))
    else:
        print(render(report, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
