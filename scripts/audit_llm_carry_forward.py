#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit LLM carry-forward across CA counties (#4289).

Issue #3649 documented an LLM carry-forward bug in Riverside multi-case PDFs:
the LLM violates rule 5b of its own prompt and copies the first entry's
``outcome``/``motion_type``/``case_title`` onto subsequent entries.

PR #4286 fixed Riverside via a deterministic pre-LLM splitter, but the root
cause is not Riverside-specific: any CA county that uses the framework
``LlmExtractor`` with a per-county prompt AND captures multi-case PDFs/HTML
without a deterministic pre-LLM splitter is at risk of the same bug shape.

This script runs four carry-forward checks per CA county:

1. **outcome_continue** — ``outcome IN
   ('granted','denied','granted_in_part','denied_in_part')`` but
   ``ruling_text`` starts with "continue"/"continued" boilerplate. The
   AC #3 axis from #3649.

2. **motion_type_contradiction** — ``motion_type`` mentions a specific
   motion (e.g. "demurrer") but the ruling_text does not contain a stem
   of that motion ("demur"). Per-keyword.

3. **case_title_text_mismatch** — significant words from ``case_title``
   ("v. plaintiff_name") do not appear in ``ruling_text``. Same shape as
   ``audit_oc_ruling_integrity.py`` but applied per-county.

4. **all_same_case_title_cluster** — same ``documents.s3_key`` has multiple
   rulings, all sharing identical ``case_title``. Strong indicator the LLM
   applied page-1 case to every entry.

Counties with deterministic splitters wired in
``ingestion/worker.py`` (Riverside, Fresno, San Diego, LA) should produce
near-zero counts in checks 1, 2, 4. Non-zero counts in those counties
indicate either the splitter regressed or there are multi-case shapes
the splitter doesn't cover.

Counties with custom LLM prompts but no splitter (San Bernardino, San
Francisco, Santa Clara, Ventura, Contra Costa, Orange-multimodal) are the
prime candidates for the bug class. Non-zero counts there should produce
follow-up issues mirroring #3649 / #3534.

Usage::

    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
        scripts/audit_llm_carry_forward.py

Options:
    --json          Machine-readable JSON output (full check details).
    --county NAME   Restrict to a single county (default: all CA counties).
    --since DATE    Only audit rulings posted_at >= DATE (default: all time).
    --limit-examples N
                    Cap stored examples per check per county (default: 5).

Exit code: 0 always — this is a discovery audit, not a CI gate. Non-zero
findings are reported via stdout / JSON / follow-up issues, not exit codes.
"""

from __future__ import annotations

import argparse
import importlib
import json as json_mod
import logging
import os
import re
import sys
from typing import Any

# psycopg is imported lazily inside ``run_audit`` so the module — and its
# helper primitives — can be imported in a test environment that doesn't
# have psycopg installed (e.g. CI's scripts-tests (python) job that runs
# pure-Python script tests without the full scraper-framework venv).
psycopg: Any = None  # populated by ``_load_psycopg``


def _load_psycopg() -> Any:
    """Lazy-load psycopg. Cached on the module to avoid repeated importlib
    overhead and to give tests a single attribute to monkey-patch.
    """
    global psycopg
    if psycopg is None:
        psycopg = importlib.import_module("psycopg")
    return psycopg


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration: per-motion-type keyword stems for check 2
# ---------------------------------------------------------------------------
# Each entry is (motion_type_pattern, regex of stems that MUST appear in
# ruling_text if motion_type matches). The pattern matches DB
# ``rulings.motion_type`` values case-insensitively. The stem regex matches
# ruling_text case-insensitively. If motion_type matches but no stem
# appears, that's a contradiction signal.
#
# Stems are deliberately broad: a real "demurrer" ruling will contain at
# least one of "demur", "demurrer", "demurrers". A motion_type='demurrer'
# label on text that doesn't contain any of those stems is a strong
# carry-forward signal.

_MOTION_TYPE_STEMS: list[tuple[re.Pattern[str], re.Pattern[str]]] = [
    # demurrer
    (
        re.compile(r"demurrer", re.IGNORECASE),
        re.compile(r"\bdemur(?:rer|rers|red|ring)?\b", re.IGNORECASE),
    ),
    # summary judgment / adjudication (MSJ / MSA)
    (
        re.compile(r"summary\s+(judgment|adjudication)|\bmsj\b|\bmsa\b", re.IGNORECASE),
        re.compile(r"summary\s+(judgment|adjudication)|\bmsj\b|\bmsa\b", re.IGNORECASE),
    ),
    # motion to compel
    (
        re.compile(r"compel", re.IGNORECASE),
        re.compile(r"\bcompel(?:s|led|ling)?\b", re.IGNORECASE),
    ),
    # motion to quash
    (
        re.compile(r"quash", re.IGNORECASE),
        re.compile(r"\bquash(?:es|ed|ing)?\b", re.IGNORECASE),
    ),
    # motion to strike
    (
        re.compile(r"\bstrike\b", re.IGNORECASE),
        re.compile(r"\bstrike|stricken|striking\b", re.IGNORECASE),
    ),
    # anti-SLAPP
    (
        re.compile(r"slapp", re.IGNORECASE),
        re.compile(r"slapp|425\.16", re.IGNORECASE),
    ),
    # motion for sanctions
    (
        re.compile(r"sanction", re.IGNORECASE),
        re.compile(r"sanction(?:s|ed|ing)?", re.IGNORECASE),
    ),
    # motion to dismiss (less common in CA, but happens)
    (
        re.compile(r"dismiss", re.IGNORECASE),
        re.compile(r"dismiss(?:al|als|ed|ing)?", re.IGNORECASE),
    ),
    # motion for new trial
    (
        re.compile(r"new\s+trial", re.IGNORECASE),
        re.compile(r"new\s+trial", re.IGNORECASE),
    ),
    # motion in limine
    (
        re.compile(r"limine", re.IGNORECASE),
        re.compile(r"limine", re.IGNORECASE),
    ),
]


# Continuance regex — same shape as #3649 AC #3.
_CONTINUE_RE = re.compile(
    r"^(?:continue\s|continued\s|"
    r"the\s+(?:motion|hearing|matter)\s+is\s+continued|"
    r"the\s+court\s+continues)",
    re.IGNORECASE,
)


_OUTCOME_DEFINITIVE = (
    "granted",
    "denied",
    "granted_in_part",
    "denied_in_part",
)


# Words to ignore when checking case_title vs ruling_text overlap.
_TITLE_NOISE_WORDS = frozenset(
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
        "court",
        "et",
        "al",
        "inc",
        "llc",
        "ltd",
        "co",
        "corp",
        "company",
        "vs",
        "v",
    }
)


_VS_RE = re.compile(r"\s+vs?\.?\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_parties_from_title(title: str | None) -> tuple[str, str] | None:
    """Return ``(plaintiff, defendant)`` lowercased, or ``None`` if unparseable."""
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


def _significant_words(name: str) -> list[str]:
    """Return useful matching words (>=3 chars, not noise)."""
    words = [
        w.strip(",.")
        for w in re.split(r"\W+", name)
        if w and w.lower() not in _TITLE_NOISE_WORDS and len(w) >= 3
    ]
    return [w for w in words if w]


def _check_outcome_continue(outcome: str | None, ruling_text: str | None) -> bool:
    """True when outcome is definitive but text starts with continuance."""
    if not outcome or outcome not in _OUTCOME_DEFINITIVE:
        return False
    if not ruling_text:
        return False
    return bool(_CONTINUE_RE.search(ruling_text.lstrip()))


def _check_motion_type_contradiction(
    motion_type: str | None, ruling_text: str | None
) -> str | None:
    """Return matching motion_type pattern if motion_type contradicts text."""
    if not motion_type or not ruling_text:
        return None
    for mt_re, stem_re in _MOTION_TYPE_STEMS:
        if mt_re.search(motion_type) and not stem_re.search(ruling_text):
            return mt_re.pattern
    return None


def _check_case_title_text_mismatch(
    case_title: str | None, ruling_text: str | None
) -> bool:
    """True when no significant case_title party word appears in ruling_text."""
    if not case_title or not ruling_text:
        return False

    # Prefer party-name extraction when "v." is present.
    parties = _extract_parties_from_title(case_title)
    if parties is not None:
        left, right = parties
        words = _significant_words(left) + _significant_words(right)
    else:
        words = _significant_words(case_title)

    if not words:
        return False

    text_lower = ruling_text.lower()
    for w in words:
        if re.search(r"\b" + re.escape(w.lower()) + r"\b", text_lower):
            return False
    return True


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Pull every CA ruling with the columns we need. We do all four checks in
# Python rather than SQL because (a) check 2 needs per-row regex over the
# motion_type/text vocabulary and (b) the dataset is small enough that
# pulling 10-100k rows over the wire is cheap relative to the ECS task
# overhead. The grouping query for check 4 happens separately.

_RULING_QUERY = """
    SELECT
        r.id::text         AS ruling_id,
        c.case_title       AS case_title,
        r.ruling_text      AS ruling_text,
        r.outcome::text    AS outcome,
        r.motion_type      AS motion_type,
        d.s3_key           AS s3_key,
        d.scraper_id       AS scraper_id,
        ct.county          AS county,
        ct.state           AS state
    FROM derived.rulings r
    JOIN derived.cases     c  ON c.id = r.case_id
    JOIN derived.courts    ct ON ct.id = r.court_id
    JOIN derived.documents d  ON d.id = r.document_id
    WHERE ct.state = 'CA'
      {county_filter}
      {since_filter}
"""

# Cluster query for check 4 — same s3_key (one source document) with
# multiple rulings, all sharing identical case_title.
#
# Single-case PDFs naturally have one (s3_key, case_title) tuple. Multi-case
# PDFs split correctly produce multiple distinct case_titles per s3_key. The
# bug shape is multi-case PDFs that produced N>=2 rulings, all with the
# SAME case_title — i.e. the LLM applied page 1's case to every entry.

_CLUSTER_QUERY = """
    SELECT
        ct.county          AS county,
        d.s3_key           AS s3_key,
        d.scraper_id       AS scraper_id,
        c.case_title       AS case_title,
        COUNT(*)           AS ruling_count
    FROM derived.rulings r
    JOIN derived.cases     c  ON c.id = r.case_id
    JOIN derived.courts    ct ON ct.id = r.court_id
    JOIN derived.documents d  ON d.id = r.document_id
    WHERE ct.state = 'CA'
      {county_filter}
      {since_filter}
      AND c.case_title IS NOT NULL
      AND c.case_title <> ''
    GROUP BY ct.county, d.s3_key, d.scraper_id, c.case_title
    HAVING COUNT(*) >= 2
       AND COUNT(DISTINCT r.id) = COUNT(*)
"""


def _build_filters(county: str | None, since: str | None) -> tuple[str, str, list]:
    """Build the dynamic WHERE clause fragments + parameter list."""
    params: list = []
    county_filter = ""
    since_filter = ""
    if county:
        county_filter = "AND UPPER(ct.county) = UPPER(%s)"
        params.append(county)
    if since:
        since_filter = "AND r.posted_at >= %s"
        params.append(since)
    return county_filter, since_filter, params


# ---------------------------------------------------------------------------
# Audit driver
# ---------------------------------------------------------------------------


def run_audit(
    dsn: str,
    *,
    county: str | None = None,
    since: str | None = None,
    limit_examples: int = 5,
) -> dict[str, Any]:
    """Run all four carry-forward checks. Returns a structured summary."""

    county_filter, since_filter, params = _build_filters(county, since)
    ruling_sql = _RULING_QUERY.format(
        county_filter=county_filter, since_filter=since_filter
    )
    cluster_sql = _CLUSTER_QUERY.format(
        county_filter=county_filter, since_filter=since_filter
    )

    # Per-county counters
    counties: dict[str, dict[str, Any]] = {}

    def _bucket(county_name: str) -> dict[str, Any]:
        if county_name not in counties:
            counties[county_name] = {
                "total_rulings": 0,
                "outcome_continue": {"count": 0, "examples": []},
                "motion_type_contradiction": {"count": 0, "examples": []},
                "case_title_text_mismatch": {"count": 0, "examples": []},
                "all_same_case_title_cluster": {"count": 0, "examples": []},
            }
        return counties[county_name]

    pg = _load_psycopg()
    with pg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Per-row checks (1, 2, 3)
            cur.execute(ruling_sql, params)
            for row in cur.fetchall():
                (
                    ruling_id,
                    case_title,
                    ruling_text,
                    outcome,
                    motion_type,
                    s3_key,
                    scraper_id,
                    county_name,
                    _state,
                ) = row
                bucket = _bucket(county_name)
                bucket["total_rulings"] += 1

                # Check 1 — outcome carry-forward (continuance)
                if _check_outcome_continue(outcome, ruling_text):
                    bucket["outcome_continue"]["count"] += 1
                    if len(bucket["outcome_continue"]["examples"]) < limit_examples:
                        bucket["outcome_continue"]["examples"].append(
                            {
                                "ruling_id": ruling_id,
                                "outcome": outcome,
                                "ruling_text_excerpt": (ruling_text or "")[:150],
                                "scraper_id": scraper_id,
                            }
                        )

                # Check 2 — motion_type contradiction
                contradiction = _check_motion_type_contradiction(
                    motion_type, ruling_text
                )
                if contradiction:
                    bucket["motion_type_contradiction"]["count"] += 1
                    if (
                        len(bucket["motion_type_contradiction"]["examples"])
                        < limit_examples
                    ):
                        bucket["motion_type_contradiction"]["examples"].append(
                            {
                                "ruling_id": ruling_id,
                                "motion_type": motion_type,
                                "expected_stem": contradiction,
                                "ruling_text_excerpt": (ruling_text or "")[:150],
                                "scraper_id": scraper_id,
                            }
                        )

                # Check 3 — case_title vs text mismatch
                if _check_case_title_text_mismatch(case_title, ruling_text):
                    bucket["case_title_text_mismatch"]["count"] += 1
                    if (
                        len(bucket["case_title_text_mismatch"]["examples"])
                        < limit_examples
                    ):
                        bucket["case_title_text_mismatch"]["examples"].append(
                            {
                                "ruling_id": ruling_id,
                                "case_title": case_title,
                                "ruling_text_excerpt": (ruling_text or "")[:150],
                                "scraper_id": scraper_id,
                            }
                        )

            # Check 4 — same-s3_key + same-case_title clusters
            cur.execute(cluster_sql, params)
            for row in cur.fetchall():
                (
                    county_name,
                    s3_key,
                    scraper_id,
                    case_title,
                    ruling_count,
                ) = row
                bucket = _bucket(county_name)
                bucket["all_same_case_title_cluster"]["count"] += 1
                if (
                    len(bucket["all_same_case_title_cluster"]["examples"])
                    < limit_examples
                ):
                    bucket["all_same_case_title_cluster"]["examples"].append(
                        {
                            "s3_key": s3_key,
                            "case_title": case_title,
                            "ruling_count": ruling_count,
                            "scraper_id": scraper_id,
                        }
                    )

    # Compose final summary
    summary: dict[str, Any] = {
        "filter": {"county": county, "since": since},
        "counties": counties,
    }
    summary["totals"] = _aggregate_totals(counties)
    summary["all_clean"] = _is_all_clean(counties)
    return summary


def _aggregate_totals(
    counties: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Sum each check's count across counties."""
    return {
        "rulings_audited": sum(int(c["total_rulings"]) for c in counties.values()),
        "outcome_continue": sum(
            int(c["outcome_continue"]["count"]) for c in counties.values()
        ),
        "motion_type_contradiction": sum(
            int(c["motion_type_contradiction"]["count"]) for c in counties.values()
        ),
        "case_title_text_mismatch": sum(
            int(c["case_title_text_mismatch"]["count"]) for c in counties.values()
        ),
        "all_same_case_title_cluster": sum(
            int(c["all_same_case_title_cluster"]["count"]) for c in counties.values()
        ),
    }


def _is_all_clean(counties: dict[str, dict[str, Any]]) -> bool:
    """True when every check has zero count across every county."""
    for c in counties.values():
        for key in (
            "outcome_continue",
            "motion_type_contradiction",
            "case_title_text_mismatch",
            "all_same_case_title_cluster",
        ):
            if c[key]["count"] > 0:
                return False
    return True


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_text_report(summary: dict[str, Any]) -> str:
    """Render a human-readable per-county summary."""
    lines: list[str] = []
    lines.append("LLM Carry-Forward Audit (#4289)")
    lines.append("=" * 60)
    flt = summary.get("filter", {})
    if flt.get("county"):
        lines.append(f"County filter: {flt['county']}")
    if flt.get("since"):
        lines.append(f"Since: {flt['since']}")
    totals = summary.get("totals", {})
    lines.append(f"Total CA rulings audited: {totals.get('rulings_audited', 0)}")
    lines.append("")

    header = (
        f"{'County':<22s} "
        f"{'Rulings':>8s} "
        f"{'OutCont':>8s} "
        f"{'MTypeMis':>9s} "
        f"{'TitleMis':>9s} "
        f"{'TitleClust':>11s}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for county_name in sorted(summary["counties"]):
        c = summary["counties"][county_name]
        lines.append(
            f"{county_name[:22]:<22s} "
            f"{c['total_rulings']:>8d} "
            f"{c['outcome_continue']['count']:>8d} "
            f"{c['motion_type_contradiction']['count']:>9d} "
            f"{c['case_title_text_mismatch']['count']:>9d} "
            f"{c['all_same_case_title_cluster']['count']:>11d}"
        )

    lines.append("")
    lines.append(
        f"TOTAL{'':<17s} "
        f"{totals.get('rulings_audited', 0):>8d} "
        f"{totals.get('outcome_continue', 0):>8d} "
        f"{totals.get('motion_type_contradiction', 0):>9d} "
        f"{totals.get('case_title_text_mismatch', 0):>9d} "
        f"{totals.get('all_same_case_title_cluster', 0):>11d}"
    )
    lines.append("")
    if summary.get("all_clean"):
        lines.append("All clean — no carry-forward signals across CA counties.")
    else:
        lines.append("Carry-forward signals detected — see per-county counts above.")
        lines.append(
            "Re-run with --json to inspect example ruling_id / s3_key / "
            "scraper_id values for each non-zero check."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit LLM carry-forward across CA counties (#4289).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full structured summary as JSON.",
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Restrict to a single county (default: all CA counties).",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only include rulings posted_at >= DATE (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--limit-examples",
        type=int,
        default=5,
        help="Cap stored examples per check per county (default: 5).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(2)

    summary = run_audit(
        dsn,
        county=args.county,
        since=args.since,
        limit_examples=args.limit_examples,
    )

    if args.json:
        print(json_mod.dumps(summary, indent=2, default=str))
    else:
        print(render_text_report(summary))

    # Discovery audit — always exit 0. Non-zero findings live in stdout / JSON,
    # not exit codes (this matches `audit_field_completeness.py` pattern).
    sys.exit(0)


if __name__ == "__main__":
    main()
