#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit OC ruling data integrity — detect misrouted and boilerplate rulings.

Queries the database for Orange County rulings and checks for two classes of
data integrity issues:

1. **Misrouted rulings** — the ruling_text mentions different party names than
   the case_title on the case record.  This catches North JC split fragments
   assigned to the wrong case (see issue #1186).

2. **Boilerplate-only rulings** — ruling_text is short (< 700 chars) and
   contains calendar header keywords ("TENTATIVE RULINGS", "MOTION") but no
   substantive ruling content.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
        scripts/audit_oc_ruling_integrity.py

Options:
    --json          Machine-readable JSON output.
    --verbose       Show full ruling_text excerpts for flagged rulings.
    --county NAME   Override the county filter (default: Orange).

Exit code: 0 if no issues found, 1 if issues detected.
"""

from __future__ import annotations

import argparse
import json as json_mod
import logging
import os
import re
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL query — fetch OC rulings with their case_title and ruling_text
# ---------------------------------------------------------------------------

AUDIT_QUERY = """
    SELECT
        r.id        AS ruling_id,
        r.case_id   AS case_id,
        c.case_title,
        r.ruling_text,
        LENGTH(r.ruling_text) AS ruling_len,
        ct.county
    FROM rulings r
    JOIN cases c  ON c.id = r.case_id
    JOIN courts ct ON ct.id = r.court_id
    WHERE ct.county = %s
      AND r.ruling_text IS NOT NULL
    ORDER BY r.created_at DESC
"""

# ---------------------------------------------------------------------------
# Party-name extraction
# ---------------------------------------------------------------------------

# Matches "v.", "vs", "vs." between party names (case-insensitive).
_VS_RE = re.compile(r"\s+vs?\.?\s+", re.IGNORECASE)


def _extract_parties_from_title(
    title: str | None,
) -> tuple[str, str] | None:
    """Extract (plaintiff, defendant) from a case title like 'Smith v. Jones'.

    Returns a tuple of lowercased, stripped party name strings, or None if the
    title does not contain a recognisable "v." / "vs" separator.
    """
    if not title:
        return None
    parts = _VS_RE.split(title.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    left = parts[0].strip().lower()
    right = parts[1].strip().lower()
    if not left or not right:
        return None
    return (left, right)


# ---------------------------------------------------------------------------
# Misroute detection
# ---------------------------------------------------------------------------


def _check_misroute(row: dict[str, object]) -> str | None:
    """Check whether a ruling's text mentions different parties than its case.

    Returns a human-readable reason string if a mismatch is detected, or None
    if the ruling looks correctly assigned (or cannot be checked).
    """
    case_title = row.get("case_title")
    ruling_text = row.get("ruling_text")
    if not case_title or not ruling_text:
        return None

    title_parties = _extract_parties_from_title(str(case_title))
    if title_parties is None:
        return None

    left, right = title_parties
    text_lower = str(ruling_text).lower()

    # Extract the first significant word from each party name (skip common
    # prefixes like "the", "people", "state") to reduce false positives.
    left_words = _significant_words(left)
    right_words = _significant_words(right)

    # If neither party has significant words (e.g. "The People v. State"),
    # we can't reliably check for misrouting — skip.
    if not left_words and not right_words:
        return None

    # Check whether at least one significant word from each party appears in
    # the ruling text.  If NEITHER party name appears, that's a strong signal
    # the ruling was misrouted.
    left_found = any(
        re.search(r"\b" + re.escape(w) + r"\b", text_lower) for w in left_words
    )
    right_found = any(
        re.search(r"\b" + re.escape(w) + r"\b", text_lower) for w in right_words
    )

    if left_found or right_found:
        return None

    return (
        f"Possible misroute: case_title parties ({left} / {right}) "
        f"not found in ruling_text"
    )


_NOISE_WORDS = frozenset(
    {
        "the",
        "of",
        "in",
        "re",
        "a",
        "an",
        "and",
        "or",
        "people",
        "state",
        "county",
        "city",
    }
)


def _significant_words(name: str) -> list[str]:
    """Return words from a party name that are useful for matching.

    Filters out common noise words and returns words >= 3 chars long.
    """
    words = [w for w in name.split() if w not in _NOISE_WORDS and len(w) >= 3]
    return words


# ---------------------------------------------------------------------------
# Boilerplate detection
# ---------------------------------------------------------------------------

_BOILERPLATE_THRESHOLD = 700
_BOILERPLATE_KEYWORDS = ["tentative rulings", "motion"]


def _check_boilerplate(row: dict[str, object]) -> str | None:
    """Check whether a ruling contains only calendar header boilerplate.

    Returns a human-readable reason string if boilerplate is detected, or None
    if the ruling has substantive content.
    """
    ruling_text = row.get("ruling_text")
    if not ruling_text:
        return None

    text = str(ruling_text)
    if len(text) > _BOILERPLATE_THRESHOLD:
        return None

    text_lower = text.lower()
    has_keywords = all(kw in text_lower for kw in _BOILERPLATE_KEYWORDS)
    if not has_keywords:
        return None

    return (
        f"Boilerplate-only ruling ({len(text)} chars): "
        f"contains header keywords but no substantive content"
    )


# ---------------------------------------------------------------------------
# Main audit logic
# ---------------------------------------------------------------------------


def run_audit(
    dsn: str,
    *,
    county: str = "Orange",
    output_json: bool = False,
    verbose: bool = False,
) -> dict:
    """Run the OC ruling integrity audit.

    Returns a dict with keys: all_clean, total_checked, misrouted, boilerplate.
    If output_json is False, also prints a human-readable report to stdout.
    """
    misrouted: list[dict] = []
    boilerplate: list[dict] = []
    total_checked = 0

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(AUDIT_QUERY, (county,))
            rows = cur.fetchall()

    for row_tuple in rows:
        total_checked += 1
        row = {
            "ruling_id": str(row_tuple[0]),
            "case_id": str(row_tuple[1]),
            "case_title": row_tuple[2],
            "ruling_text": row_tuple[3],
            "ruling_len": row_tuple[4],
            "county": row_tuple[5],
        }

        misroute_reason = _check_misroute(row)
        if misroute_reason:
            entry = {
                "ruling_id": row["ruling_id"],
                "case_id": row["case_id"],
                "case_title": row["case_title"],
                "reason": misroute_reason,
            }
            if verbose:
                text = row["ruling_text"] or ""
                entry["ruling_text_excerpt"] = str(text)[:300]
            misrouted.append(entry)

        bp_reason = _check_boilerplate(row)
        if bp_reason:
            entry = {
                "ruling_id": row["ruling_id"],
                "case_id": row["case_id"],
                "case_title": row["case_title"],
                "reason": bp_reason,
                "ruling_len": row["ruling_len"],
            }
            if verbose:
                text = row["ruling_text"] or ""
                entry["ruling_text_excerpt"] = str(text)[:300]
            boilerplate.append(entry)

    result = {
        "all_clean": len(misrouted) == 0 and len(boilerplate) == 0,
        "total_checked": total_checked,
        "misrouted": misrouted,
        "boilerplate": boilerplate,
    }

    if output_json:
        return result

    # Human-readable output
    _print_report(result, verbose=verbose)
    return result


def _print_report(result: dict, *, verbose: bool = False) -> None:
    """Print a human-readable audit report to stdout."""
    total = result["total_checked"]
    misrouted = result["misrouted"]
    boilerplate = result["boilerplate"]

    print("OC Ruling Integrity Audit")
    print(f"{'=' * 50}")
    print(f"Total rulings checked: {total}")
    print()

    print(f"Misrouted rulings: {len(misrouted)}")
    if misrouted:
        print(f"  {'Ruling ID':<40s} {'Case Title':<40s}")
        print(f"  {'-' * 40} {'-' * 40}")
        for entry in misrouted:
            print(
                f"  {entry['ruling_id']:<40s} "
                f"{(entry['case_title'] or 'N/A')[:40]:<40s}"
            )
            if verbose and "ruling_text_excerpt" in entry:
                print(f"    Text: {entry['ruling_text_excerpt'][:80]}...")
    print()

    print(f"Boilerplate-only rulings: {len(boilerplate)}")
    if boilerplate:
        print(f"  {'Ruling ID':<40s} {'Chars':<8s} {'Case Title':<40s}")
        print(f"  {'-' * 40} {'-' * 8} {'-' * 40}")
        for entry in boilerplate:
            print(
                f"  {entry['ruling_id']:<40s} "
                f"{entry['ruling_len']:<8} "
                f"{(entry['case_title'] or 'N/A')[:40]:<40s}"
            )
            if verbose and "ruling_text_excerpt" in entry:
                print(f"    Text: {entry['ruling_text_excerpt'][:80]}...")
    print()

    if result["all_clean"]:
        print("All rulings passed integrity checks!")
    else:
        issues = len(misrouted) + len(boilerplate)
        print(f"{issues} issue(s) found — see above.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit OC ruling data integrity.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show ruling text excerpts."
    )
    parser.add_argument(
        "--county",
        type=str,
        default="Orange",
        help="County to audit (default: Orange).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    result = run_audit(
        dsn,
        county=args.county,
        output_json=args.json,
        verbose=args.verbose,
    )

    if args.json:
        print(json_mod.dumps(result, indent=2))

    sys.exit(0 if result["all_clean"] else 1)


if __name__ == "__main__":
    main()
