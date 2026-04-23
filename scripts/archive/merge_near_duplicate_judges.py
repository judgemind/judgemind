#!/usr/bin/env python3
# venv: scraper-framework
"""Merge near-duplicate judge records within the same court.

After cleanup_judge_names.py handles exact duplicates and backfill_judge_names.py
fixes truncated names, there may still be near-duplicate records for the same
judge where the names differ slightly:

1. **Missing middle initial:** "Carmen Luege" vs "Carmen R. Luege"
2. **First initial vs full name:** "D. Moses" vs "Jared D. Moses"
3. **Last name only vs full name:** "Crowfoot" vs "William A. Crowfoot"
4. **Truncated names:** "Lindsey E. Mart" vs "Lindsey E. Martinez"
5. **Accent variants:** "Martinez" vs "Martinez" (with accented i)

The script finds these near-duplicate groups and merges them, keeping the most
complete name variant and moving all rulings/aliases/case_judges from the
duplicate to the survivor.

Matching algorithm:
- Group judges by court_id and last name (the final word in canonical_name).
- Within each group, compare all pairs. A pair is a near-duplicate if:
  a) One name is a prefix of the other (truncation), OR
  b) They share the same last name and one first+middle is a subset of the other
     (missing middle initial, first initial vs full first name), OR
  c) One is the unaccented version of the other (accent normalization).

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
            scripts/merge_near_duplicate_judges.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
            scripts/merge_near_duplicate_judges.py --dry-run

Options:
    --dry-run       Print what would be changed without writing to the database.
    --court CODE    Only process judges at a specific court_code.
    --limit N       Maximum total merge operations (default: unlimited).

The script is idempotent -- re-running it will find no near-duplicates if all
have already been merged.

Parent issue: #1106
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import unicodedata

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "packages",
        "scraper-framework",
        "src",
    ),
)

import psycopg  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Name comparison utilities
# ---------------------------------------------------------------------------


def _strip_accents(s: str) -> str:
    """Remove diacritical marks from a string (e.g. 'Martinez' -> 'Martinez')."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


def _last_name(name: str) -> str:
    """Extract the last name (final word, ignoring generational suffixes)."""
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
    words = name.split()
    # Walk backwards past generational suffixes to find the last name
    for i in range(len(words) - 1, -1, -1):
        if words[i].lower().rstrip(",") not in suffixes:
            return words[i].lower()
    return words[-1].lower() if words else ""


def _first_middle(name: str) -> list[str]:
    """Extract first and middle name parts (everything before the last name)."""
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
    words = name.split()
    # Find the last name index (last word that's not a suffix)
    last_idx = len(words) - 1
    for i in range(len(words) - 1, -1, -1):
        if words[i].lower().rstrip(",") not in suffixes:
            last_idx = i
            break
    return [w.lower() for w in words[:last_idx]]


def _is_initial(word: str) -> bool:
    """Check if a word is a name initial (e.g. 'D.' or 'D')."""
    stripped = word.rstrip(".")
    return len(stripped) == 1 and stripped.isalpha()


def _initial_matches(initial: str, full_name: str) -> bool:
    """Check if an initial matches the start of a full name."""
    return full_name.lower().startswith(initial.rstrip(".").lower())


def is_near_duplicate(name_a: str, name_b: str) -> bool:
    """Determine if two judge names are near-duplicates of the same person.

    Returns True if the names plausibly refer to the same person based on:
    - One being a prefix/truncation of the other
    - Same last name with compatible first/middle names
    - Accent-only differences
    """
    if not name_a or not name_b:
        return False

    # Normalize accents for comparison
    a_stripped = _strip_accents(name_a)
    b_stripped = _strip_accents(name_b)

    # Exact match after accent stripping
    if a_stripped.lower() == b_stripped.lower():
        return True

    # Check if one is a prefix/truncation of the other (minimum 3 chars)
    a_lower = a_stripped.lower()
    b_lower = b_stripped.lower()
    if len(a_lower) >= 3 and b_lower.startswith(a_lower):
        return True
    if len(b_lower) >= 3 and a_lower.startswith(b_lower):
        return True

    # Same last name comparison — also accept truncated last names
    last_a = _last_name(a_stripped)
    last_b = _last_name(b_stripped)

    last_names_match = last_a == last_b
    if not last_names_match:
        # Check if one last name is a prefix of the other (truncation)
        if len(last_a) >= 2 and last_b.startswith(last_a):
            last_names_match = True
        elif len(last_b) >= 2 and last_a.startswith(last_b):
            last_names_match = True

    if not last_names_match:
        return False

    # Last names match -- now compare first/middle names
    fm_a = _first_middle(a_stripped)
    fm_b = _first_middle(b_stripped)

    # One name is last-name-only (no first/middle)
    if not fm_a or not fm_b:
        return True

    # Check compatibility using subsequence matching. The shorter name's parts
    # must all appear (in order) in the longer name's parts, allowing:
    #   - Exact matches
    #   - Initial-to-full matches ("D." matches "David")
    #   - Extra parts in the longer name (missing middle initial)
    shorter, longer = (fm_a, fm_b) if len(fm_a) <= len(fm_b) else (fm_b, fm_a)

    def _parts_match(a: str, b: str) -> bool:
        """Check if two name parts are compatible."""
        if a == b:
            return True
        if _is_initial(a) and _initial_matches(a, b):
            return True
        if _is_initial(b) and _initial_matches(b, a):
            return True
        return False

    # Try to match all shorter parts as a subsequence of longer parts
    longer_idx = 0
    matched = 0
    for short_part in shorter:
        while longer_idx < len(longer):
            if _parts_match(short_part, longer[longer_idx]):
                matched += 1
                longer_idx += 1
                break
            longer_idx += 1

    return matched == len(shorter)


def pick_survivor(
    judges: list[dict[str, str | int]],
) -> tuple[dict[str, str | int], list[dict[str, str | int]]]:
    """Pick the best name variant to keep and return (survivor, duplicates).

    Prefers: most complete name (longest canonical_name after accent stripping),
    then most rulings as tiebreaker.
    """

    def sort_key(j: dict[str, str | int]) -> tuple[int, int]:
        name = str(j["canonical_name"])
        # Prefer names with more words (more complete)
        word_count = len(name.split())
        ruling_count = int(j["ruling_count"])
        return (word_count, ruling_count)

    sorted_judges = sorted(judges, key=sort_key, reverse=True)
    return sorted_judges[0], sorted_judges[1:]


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


def _fetch_judges(
    conn: psycopg.Connection,
    court_code: str | None,
) -> list[dict[str, str | int]]:
    """Fetch all judges with their ruling counts."""
    sql = """
        SELECT j.id, j.canonical_name, j.court_id, c.court_code,
               COUNT(r.id) AS ruling_count
        FROM judges j
        JOIN courts c ON c.id = j.court_id
        LEFT JOIN rulings r ON r.judge_id = j.id
    """
    params: list[str] = []
    if court_code:
        sql += " WHERE c.court_code = %s"
        params.append(court_code)
    sql += " GROUP BY j.id, j.canonical_name, j.court_id, c.court_code"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {
            "id": str(row[0]),
            "canonical_name": row[1],
            "court_id": str(row[2]),
            "court_code": row[3],
            "ruling_count": row[4],
        }
        for row in rows
    ]


def _merge_judge(
    conn: psycopg.Connection,
    from_id: str,
    to_id: str,
    dry_run: bool,
) -> int:
    """Merge judge from_id into to_id: re-link rulings, aliases, case_judges.

    Returns the number of rulings re-linked.
    """
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rulings WHERE judge_id = %s",
                (from_id,),
            )
            row = cur.fetchone()
            count = row[0] if row else 0
        logger.info(
            "  [DRY RUN] Would merge judge %s -> %s (%d rulings)",
            from_id,
            to_id,
            count,
        )
        return count

    with conn.cursor() as cur:
        # Re-link rulings
        cur.execute(
            "UPDATE rulings SET judge_id = %s WHERE judge_id = %s",
            (to_id, from_id),
        )
        rulings_updated = cur.rowcount

        # Re-link case_judges (skip conflicts)
        cur.execute(
            """
            UPDATE case_judges SET judge_id = %s
            WHERE judge_id = %s
            AND NOT EXISTS (
                SELECT 1 FROM case_judges cj2
                WHERE cj2.case_id = case_judges.case_id AND cj2.judge_id = %s
            )
            """,
            (to_id, from_id, to_id),
        )

        # Delete remaining case_judges for old judge (conflicts)
        cur.execute("DELETE FROM case_judges WHERE judge_id = %s", (from_id,))

        # Re-link judge_aliases (skip conflicts)
        cur.execute(
            """
            UPDATE judge_aliases SET judge_id = %s
            WHERE judge_id = %s
            AND NOT EXISTS (
                SELECT 1 FROM judge_aliases ja2
                WHERE ja2.raw_name = judge_aliases.raw_name AND ja2.judge_id = %s
            )
            """,
            (to_id, from_id, to_id),
        )

        # Delete remaining aliases for old judge (conflicts)
        cur.execute("DELETE FROM judge_aliases WHERE judge_id = %s", (from_id,))

        # Delete the old judge record
        cur.execute("DELETE FROM judges WHERE id = %s", (from_id,))

    logger.info(
        "  Merged judge %s -> %s (%d rulings re-linked)",
        from_id,
        to_id,
        rulings_updated,
    )
    return rulings_updated


def find_near_duplicate_groups(
    judges: list[dict[str, str | int]],
) -> list[list[dict[str, str | int]]]:
    """Find groups of near-duplicate judges within the same court.

    Groups judges by court_id, then checks all pairs within each court for
    near-duplicate status using union-find to cluster connected duplicates.
    Returns a list of groups where each group contains 2+ near-duplicate records.

    This uses O(n^2) pairwise comparison within each court, which is fine for
    the expected scale (~200 judges across ~6 courts).
    """
    # Group by court_id
    by_court: dict[str, list[dict[str, str | int]]] = {}
    for judge in judges:
        court_id = str(judge["court_id"])
        if court_id not in by_court:
            by_court[court_id] = []
        by_court[court_id].append(judge)

    # Within each court, find connected near-duplicate clusters
    result: list[list[dict[str, str | int]]] = []
    for court_judges in by_court.values():
        if len(court_judges) < 2:
            continue

        # Use union-find to cluster near-duplicates
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for i in range(len(court_judges)):
            for j in range(i + 1, len(court_judges)):
                name_i = str(court_judges[i]["canonical_name"])
                name_j = str(court_judges[j]["canonical_name"])
                if is_near_duplicate(name_i, name_j):
                    union(str(court_judges[i]["id"]), str(court_judges[j]["id"]))

        # Collect clusters
        clusters: dict[str, list[dict[str, str | int]]] = {}
        for judge in court_judges:
            root = find(str(judge["id"]))
            if root not in clusters:
                clusters[root] = []
            clusters[root].append(judge)

        for cluster in clusters.values():
            if len(cluster) >= 2:
                result.append(cluster)

    return result


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    """Main merge logic."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)

    try:
        judges = _fetch_judges(conn, args.court)
        logger.info("Found %d judge records to scan for near-duplicates", len(judges))

        groups = find_near_duplicate_groups(judges)
        logger.info("Found %d near-duplicate groups", len(groups))

        total_merged = 0
        total_rulings_moved = 0

        for group in groups:
            if args.limit is not None and total_merged >= args.limit:
                logger.info("Reached merge limit of %d, stopping", args.limit)
                break

            names = [str(j["canonical_name"]) for j in group]
            logger.info(
                "Near-duplicate group (court=%s): %s",
                group[0]["court_code"],
                " | ".join(names),
            )

            survivor, duplicates = pick_survivor(group)
            logger.info(
                "  Keeping: %r (id=%s, rulings=%d)",
                survivor["canonical_name"],
                survivor["id"],
                survivor["ruling_count"],
            )

            for dup in duplicates:
                if args.limit is not None and total_merged >= args.limit:
                    break
                logger.info(
                    "  Merging: %r (id=%s, rulings=%d) -> %r",
                    dup["canonical_name"],
                    dup["id"],
                    dup["ruling_count"],
                    survivor["canonical_name"],
                )
                rulings = _merge_judge(
                    conn,
                    str(dup["id"]),
                    str(survivor["id"]),
                    args.dry_run,
                )
                total_merged += 1
                total_rulings_moved += rulings

        if not args.dry_run:
            conn.commit()
            logger.info("Changes committed.")
        else:
            conn.rollback()
            logger.info("[DRY RUN] No changes written.")

        logger.info("Summary:")
        logger.info("  Near-duplicate groups found: %d", len(groups))
        logger.info("  Judges merged:               %d", total_merged)
        logger.info("  Rulings re-linked:           %d", total_rulings_moved)

    finally:
        conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge near-duplicate judge records.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing to the database.",
    )
    parser.add_argument(
        "--court",
        type=str,
        default=None,
        help="Only process judges at a specific court_code.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total merge operations (default: unlimited).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(_parse_args())
