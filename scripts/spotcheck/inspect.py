#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Filter and summarise ``run_spotcheck.py`` JSON results.

After ``scripts/ecs-run-task.sh scripts/spotcheck/run_spotcheck.py -- --n 10
--county Orange`` writes a result to ``s3://<archive-bucket>/spotcheck/<TS>.json``,
this script answers ad-hoc "is the data quality OK in scope X?" questions
without writing custom one-liners.

Examples (#3629 verification case):

    # Are there UNKNOWN samples in the regressed Orange-County department set?
    scripts/run-py.sh scripts/spotcheck/inspect.py \\
        s3://judgemind-document-archive-dev/spotcheck/20260506T161159Z.json \\
        --county Orange --unknown-only \\
        --department-in C20 C23 C25 C27 C31 C32 CM2 C34 N15

    # All UNKNOWN samples across every county in the result.
    scripts/run-py.sh scripts/spotcheck/inspect.py tmp/spotcheck/data.json \\
        --unknown-only

    # Pipe filtered rows as JSON for further processing.
    scripts/run-py.sh scripts/spotcheck/inspect.py s3://.../result.json \\
        --null-judge-only --format json | jq '.[].case_number'

The result JSON shape comes from ``run_spotcheck.py``: each county has a
``rulings_by_county[<county>]`` list of ruling dicts, and each
``originals_by_county[<county>][i]['derived_rulings']`` list of ruling dicts
from the bidirectional originals side. ``inspect.py`` walks both sides so a
single filter pass surfaces hits from either direction.

See: https://github.com/judgemind/judgemind/issues/4235
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pure helpers — testable without boto3
# ---------------------------------------------------------------------------


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key/path`` into ``(bucket, key)``.

    Raises ``ValueError`` if the URI is malformed.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an S3 URI: {uri!r}")
    rest = uri[len("s3://") :]
    if "/" not in rest:
        raise ValueError(f"S3 URI missing key: {uri!r}")
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"S3 URI bucket or key empty: {uri!r}")
    return bucket, key


def is_s3_uri(uri: str) -> bool:
    """Return True if ``uri`` looks like an ``s3://...`` URI."""
    return uri.startswith("s3://")


def iter_rulings(
    result: dict[str, Any],
    counties: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Walk both sides of a ``run_spotcheck.py`` result and yield ruling dicts.

    Each yielded dict is augmented with two keys for downstream display:

    - ``__source``: ``"rulings"`` or ``"originals.derived"`` — which side of
      the bidirectional spotcheck the row came from. Useful for diagnosing
      which sampling path surfaced a problem.
    - ``__county``: county name from the enclosing dict (the rows themselves
      do not carry county directly).

    Args:
        result: Parsed JSON from ``run_spotcheck.py``'s S3 output.
        counties: If non-empty, restrict to these county names
            (case-insensitive). ``None``/empty walks every county present.

    Returns:
        Flat list of ruling dicts with the two ``__source`` / ``__county``
        keys added.
    """
    wanted: set[str] | None = None
    if counties:
        wanted = {c.lower() for c in counties}

    rows: list[dict[str, Any]] = []

    for county, rulings in (result.get("rulings_by_county") or {}).items():
        if wanted is not None and county.lower() not in wanted:
            continue
        for r in rulings or []:
            row = dict(r)
            row["__source"] = "rulings"
            row["__county"] = county
            rows.append(row)

    for county, originals in (result.get("originals_by_county") or {}).items():
        if wanted is not None and county.lower() not in wanted:
            continue
        for orig in originals or []:
            for r in orig.get("derived_rulings") or []:
                row = dict(r)
                row["__source"] = "originals.derived"
                row["__county"] = county
                # Originals' derived rulings don't carry s3_key directly —
                # surface the parent original's s3_key for display so the
                # operator can locate the source document quickly.
                if orig.get("s3_key") and "s3_key" not in row:
                    row["s3_key"] = orig["s3_key"]
                rows.append(row)

    return rows


def is_unknown_case_number(row: dict[str, Any]) -> bool:
    """Return True if ``row['case_number']`` is an UNKNOWN-* placeholder."""
    cn = row.get("case_number")
    return isinstance(cn, str) and cn.startswith("UNKNOWN")


def is_null_judge(row: dict[str, Any]) -> bool:
    """Return True if ``row['judge_name']`` is missing/empty."""
    j = row.get("judge_name")
    return j is None or (isinstance(j, str) and not j.strip())


def is_null_department(row: dict[str, Any]) -> bool:
    """Return True if ``row['department']`` is missing/empty."""
    d = row.get("department")
    return d is None or (isinstance(d, str) and not d.strip())


def is_empty_text(row: dict[str, Any]) -> bool:
    """Return True if ``row['ruling_text_length']`` is 0/missing."""
    n = row.get("ruling_text_length")
    return n is None or n == 0


def in_departments(row: dict[str, Any], departments: list[str]) -> bool:
    """Return True if ``row['department']`` matches any of ``departments``.

    Case-sensitive (court department codes are codes, not names). A row whose
    department is null/empty matches only if the literal string ``null`` is in
    the list — operators filtering for null department should use
    ``--null-department-only`` instead.
    """
    if not departments:
        return True  # no filter active
    dept = row.get("department")
    if dept is None:
        return "null" in departments
    return dept in departments


def filter_rows(
    rows: list[dict[str, Any]],
    *,
    unknown_only: bool = False,
    null_judge_only: bool = False,
    null_department_only: bool = False,
    empty_text_only: bool = False,
    departments: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Apply filters in AND fashion. All filters compose."""
    out = []
    depts = departments or []
    for row in rows:
        if unknown_only and not is_unknown_case_number(row):
            continue
        if null_judge_only and not is_null_judge(row):
            continue
        if null_department_only and not is_null_department(row):
            continue
        if empty_text_only and not is_empty_text(row):
            continue
        if depts and not in_departments(row, depts):
            continue
        out.append(row)
    return out


def render_table(rows: list[dict[str, Any]]) -> str:
    """Render filtered rows as a pipe-separated table.

    Columns: county | source | dept | case_number | case_title | s3_key.
    Truncates long fields to keep the table readable; full data is
    available via ``--format json``.
    """
    if not rows:
        return "(no rows)"

    columns = [
        ("county", "__county", 16),
        ("source", "__source", 18),
        ("dept", "department", 6),
        ("case_number", "case_number", 22),
        ("case_title", "case_title", 50),
        ("s3_key", "s3_key", 60),
    ]

    def cell(row: dict[str, Any], key: str, width: int) -> str:
        v = row.get(key)
        if v is None:
            v = "null"
        s = str(v)
        if len(s) > width:
            s = s[: width - 1] + "…"
        return s.ljust(width)

    header = " | ".join(name.ljust(width) for name, _, width in columns)
    sep = "-+-".join("-" * width for _, _, width in columns)
    lines = [header, sep]
    for row in rows:
        lines.append(" | ".join(cell(row, key, width) for _, key, width in columns))
    lines.append("")
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O — load from S3 URI or local path
# ---------------------------------------------------------------------------


def load_result(source: str) -> dict[str, Any]:
    """Load the spotcheck JSON from an S3 URI or a local file path.

    S3 URIs are auto-resolved via ``boto3`` — no need to ``aws s3 cp`` first.
    """
    if is_s3_uri(source):
        bucket, key = parse_s3_uri(source)
        # Lazy import: ``inspect.py`` is importable in test envs without boto3.
        import boto3  # noqa: PLC0415

        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        return json.loads(body)

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Spotcheck file not found: {source}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Filter and summarise run_spotcheck.py JSON results "
            "(s3:// URI or local path). See #4235."
        ),
    )
    parser.add_argument(
        "source",
        help="S3 URI (s3://bucket/key/...) or local path to spotcheck JSON.",
    )
    parser.add_argument(
        "--county",
        action="append",
        default=[],
        help=(
            "Restrict to one county (repeatable). Default: every county in the result."
        ),
    )
    parser.add_argument(
        "--unknown-only",
        action="store_true",
        help="Keep only rows where case_number is UNKNOWN-*.",
    )
    parser.add_argument(
        "--null-judge-only",
        action="store_true",
        help="Keep only rows where judge_name is null/empty.",
    )
    parser.add_argument(
        "--null-department-only",
        action="store_true",
        help="Keep only rows where department is null/empty.",
    )
    parser.add_argument(
        "--empty-text-only",
        action="store_true",
        help="Keep only rows where ruling_text_length is 0/missing.",
    )
    parser.add_argument(
        "--department-in",
        nargs="+",
        default=[],
        metavar="DEPT",
        help="Keep only rows whose department is in this list (case-sensitive).",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = load_result(args.source)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    rows = iter_rulings(result, counties=args.county or None)
    filtered = filter_rows(
        rows,
        unknown_only=args.unknown_only,
        null_judge_only=args.null_judge_only,
        null_department_only=args.null_department_only,
        empty_text_only=args.empty_text_only,
        departments=args.department_in,
    )

    if args.format == "json":
        # Strip the ``__`` augment keys from the JSON output so downstream
        # consumers see the original ruling shape, but preserve county /
        # source as plain keys for traceability.
        cleaned = []
        for row in filtered:
            out = {k: v for k, v in row.items() if not k.startswith("__")}
            out["county"] = row.get("__county")
            out["source"] = row.get("__source")
            cleaned.append(out)
        print(json.dumps(cleaned, indent=2, default=str))
    else:
        print(render_table(filtered))

    return 0


if __name__ == "__main__":
    sys.exit(main())
