#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Zero-record streak check — detects scrapers silently outputting no data.

Queries ``telemetry.scraper_runs`` for each active scraper and counts the
number of most-recent consecutive runs where ``records_captured = 0``. A
streak above the configured threshold fires an alert regardless of the
``status`` column — the #2620 / SF-civil incident showed that a scraper can
legitimately report ``status=success`` while producing zero records for days
(silent outage). This is the structural guard against that failure mode
across every registered scraper.

See https://github.com/judgemind/judgemind/issues/2666 for context and
https://github.com/judgemind/judgemind/issues/2620 for the originating
incident.

Streak-counting rules:

* Runs are ordered by ``started_at DESC``.
* The streak counts the most-recent uninterrupted run of ``records_captured
  = 0`` rows. As soon as a row with ``records_captured > 0`` is seen the
  streak is broken.
* If the scraper has **no** historical run with ``records_captured > 0`` it
  may be reported either as ``insufficient_history`` (never a breach) or as a
  ``never_captured`` breach, depending on age and run count (see below). This
  distinction avoids false positives on brand-new scrapers while still
  catching the silent-outage-since-deploy case (#4611).
* A ``never_captured`` breach fires for a registered, scheduled, non-excluded
  scraper that has captured 0 records on **every** run in its **entire**
  history, **and** whose oldest in-window run is at least
  ``--never-captured-min-age-days`` old, **and** which has at least
  ``max(applicable_threshold, --never-captured-min-runs)`` runs. Younger /
  barely-run never-captured scrapers stay ``insufficient_history`` to preserve
  the brand-new false-positive guard.
* When all in-window runs are zero, the unbounded ``scraper_runs`` history is
  probed (``_ever_captured``) to tell a true never-captured scraper from a long
  ``streak`` breach whose last nonzero run predates the lookback window (e.g.
  15 days of zeros with a 14d lookback). If the scraper ever captured, it is a
  ``streak`` breach (gated on ``applicable_threshold``), NOT ``never_captured``
  — this keeps the AC#3 distinction correct.
* This is distinct from the ordinary ``streak`` breach (started capturing, then
  stopped); the ``category`` field on each breach names which is which.
* An explicit ``EXCLUSIONS`` allowlist suppresses specific scraper IDs
  regardless of streak length (e.g. roster / directory scrapers that
  legitimately emit zero-record runs). EXCLUSIONS suppress never_captured
  breaches too.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
        scripts/check-scraper-zero-record-streak.py

Options:
    --json              Machine-readable JSON output (default).
    --text              Human-readable text output.
    --threshold N       Override the default streak threshold (default: 3).
    --daily-threshold N Override the daily-scraper threshold (default: 2).
    --scraper ID        Check only the specified scraper ID.
    --lookback-days N   Runs older than this are ignored (default: 14).
    --never-captured-min-age-days N  Min age before a never-captured scraper
                        breaches (default: 3).
    --never-captured-min-runs N      Min run count before a never-captured
                        scraper breaches (default: 3).

Exit code: 0 if no breaches, 1 if one or more scrapers are in breach.

Note:
    The dev database is in a private VPC. Run via
    ``scripts/ecs-run-task.sh`` or the GH Actions workflow
    ``scraper-zero-record-check.yml``, not locally.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

# Use ``configure_structlog`` so any ``extra=`` fields passed to logger calls
# surface in CloudWatch Logs Insights output. ``stdlib_bridge=True`` routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
# extras as one event per line. The previous ``logging.basicConfig`` format
# string silently dropped every ``extra=`` field — see #4368 / #4373.
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults & per-scraper configuration
# ---------------------------------------------------------------------------

# Default streak threshold for scrapers running more than once per day
# (e.g. frequent polling). 3 consecutive zero-record runs is the breach.
DEFAULT_STREAK_THRESHOLD = 3

# Daily scrapers breach after 2 consecutive zero-record runs — two missed
# days is already ~48h of silent outage, which we never want to tolerate.
DEFAULT_DAILY_STREAK_THRESHOLD = 2

# How far back to pull runs when computing the streak. 14 days is more than
# enough for any daily or frequent scraper and caps query cost.
DEFAULT_LOOKBACK_DAYS = 14

# Never-captured breach thresholds. A registered, scheduled, non-excluded
# scraper that has produced 0 records on EVERY run in its history is a silent
# outage that started at birth (#4611). It breaches only once it is both old
# enough (so genuinely-new scrapers are not flagged) AND has accumulated
# enough runs (so a scraper with a couple of runs is not flagged).
DEFAULT_NEVER_CAPTURED_MIN_AGE_DAYS = 3
DEFAULT_NEVER_CAPTURED_MIN_RUNS = 3

# Scrapers that legitimately produce zero-record runs by design.
# Each entry must include a rationale comment so future maintainers can
# understand why the scraper is excluded from the check.
EXCLUSIONS: set[str] = {
    # Court directory / roster scrapers only capture when the roster file
    # changes — zero-record runs are the *expected* steady state. These
    # scrapers do not emit to ``scraper_runs`` today, but are listed for
    # documentation; if they are ever wired in they must stay excluded.
    "ca-oc-dept-judges",
    "ca-la-dept-judges",
    "ca-fresno-dept-judges",
    "ca-kern-dept-judges",
    "ca-sd-dept-judges",
    "ca-sb-dept-judges",
    "ca-ventura-dept-judges",
    "ca-sf-dept-judges",
    "ca-riverside-dept-judges",
    # Governor appointments scraper runs quarterly-ish; most runs legitimately
    # return zero records. Exclude until dedicated health signal is wired.
    "ca-governor-appointments",
    # CourtListener federal-opinions scraper retired per #4571 / #4474 — its
    # envelopes drove dev ElastiCache OOM and wedged the dispatcher. Kept in
    # EXCLUSIONS so the silent-outage guard does not alert on the now-retired
    # (de-registered) scraper.
    "federal-courtlistener-opinions",
}

# Scrapers that run more than once per day ("frequent"). All others are
# treated as daily and use ``DEFAULT_DAILY_STREAK_THRESHOLD``. The list is
# derived from the per-scraper ``poll_interval_seconds`` in each court
# module's ``default_config`` — kept here as a flat lookup so the check
# does not need to import every scraper module.
FREQUENT_SCRAPER_IDS: set[str] = {
    # Add scraper IDs here when they poll more than once per day.
    # All current scrapers poll daily, so this set is intentionally empty.
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Breach:
    """A single zero-record streak breach."""

    scraper_id: str
    streak: int
    threshold: int
    first_zero_at: str  # ISO-8601
    last_zero_at: str  # ISO-8601
    last_nonzero_at: str | None  # ISO-8601 or None if none seen in window
    message: str
    # Breach category — defaulted so existing positional constructions and the
    # ECS runner keep working. ``streak`` = started capturing then stopped;
    # ``never_captured`` = registered/scheduled but 0 records since deploy
    # (#4611). ``format_json``'s ``asdict`` surfaces it automatically.
    category: str = "streak"


@dataclass
class InsufficientHistory:
    """Scraper with zero-record runs but no historical nonzero runs."""

    scraper_id: str
    zero_run_count: int
    message: str


@dataclass
class CheckResult:
    """Full result of a zero-record streak check."""

    breaches: list[Breach]
    insufficient_history: list[InsufficientHistory]
    checked_count: int
    excluded_count: int
    unknown_scrapers: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

# Pull recent runs for every scraper in one round trip so we don't issue
# N queries. Ordered by (scraper_id, started_at DESC) so the streak
# computation can iterate each scraper's slice without re-sorting.
RECENT_RUNS_QUERY = """
    SELECT scraper_id, started_at, records_captured, status
    FROM scraper_runs
    WHERE started_at >= %s
    {scraper_filter}
    ORDER BY scraper_id, started_at DESC
"""


def _fetch_recent_runs(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    cutoff: datetime,
    scraper_id: str | None,
) -> dict[str, list[tuple[datetime, int, str]]]:
    """Return ``{scraper_id: [(started_at, records, status), ...]}`` sorted
    by ``started_at`` descending for each scraper.
    """
    params: list[Any] = [cutoff]
    scraper_filter = ""
    if scraper_id:
        scraper_filter = "AND scraper_id = %s"
        params.append(scraper_id)

    out: dict[str, list[tuple[datetime, int, str]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            RECENT_RUNS_QUERY.format(scraper_filter=scraper_filter),
            tuple(params),
        )
        for row in cur.fetchall():
            sid, started_at, records, status = row[0], row[1], row[2], row[3]
            out.setdefault(sid, []).append((started_at, int(records), status))
    return out


# ---------------------------------------------------------------------------
# Registry helpers — known scraper IDs
# ---------------------------------------------------------------------------


def _load_known_scraper_ids() -> set[str] | None:
    """Return the set of all registered scraper IDs from framework.runner.

    Returns ``None`` when the framework package is not importable (e.g. when
    running in an environment without the scraper-framework venv active).  In
    that case callers should treat *all* fetched IDs as known to avoid false
    positives.
    """
    try:
        from framework.runner import get_scraper_ids  # type: ignore[import-not-found]

        return set(get_scraper_ids())
    except ImportError:
        logger.debug(
            "framework.runner not importable — unknown-scraper check skipped. "
            "Activate the scraper-framework venv to enable this check."
        )
        return None


# ---------------------------------------------------------------------------
# Streak logic
# ---------------------------------------------------------------------------


def compute_streak(
    runs: list[tuple[datetime, int, str]],
) -> tuple[int, datetime | None, datetime | None, datetime | None]:
    """Count most-recent consecutive zero-record runs.

    Args:
        runs: ``[(started_at, records_captured, status), ...]`` ordered by
            ``started_at`` descending.

    Returns:
        Tuple ``(streak, first_zero_at, last_zero_at, last_nonzero_at)``:
          * ``streak`` — length of the leading run of zero-record rows.
          * ``first_zero_at`` — oldest ``started_at`` inside the streak
            (i.e. when the streak began).
          * ``last_zero_at`` — newest ``started_at`` inside the streak.
          * ``last_nonzero_at`` — newest ``started_at`` with records > 0
            anywhere in the window, or ``None`` if no such run exists.
    """
    streak = 0
    first_zero_at: datetime | None = None
    last_zero_at: datetime | None = None
    last_nonzero_at: datetime | None = None
    in_leading_streak = True

    for started_at, records, _status in runs:
        if in_leading_streak and records == 0:
            streak += 1
            if last_zero_at is None:
                last_zero_at = started_at  # first iter = newest zero row
            first_zero_at = started_at  # keep overwriting = oldest zero row
        else:
            in_leading_streak = False
            if records > 0 and last_nonzero_at is None:
                last_nonzero_at = started_at

    return streak, first_zero_at, last_zero_at, last_nonzero_at


def has_historical_nonzero(runs: list[tuple[datetime, int, str]]) -> bool:
    """True if any run in the lookup window produced >0 records."""
    return any(records > 0 for _, records, _ in runs)


# Unbounded (no time filter) probe: has this scraper EVER captured? Used to
# distinguish a true never-captured scraper from a long streak whose last
# nonzero run predates the lookback window (#4611).
EVER_CAPTURED_QUERY = (
    "SELECT 1 FROM scraper_runs WHERE scraper_id = %s AND records_captured > 0 LIMIT 1"
)


def _ever_captured(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    scraper_id: str,
) -> bool:
    """True if the scraper has ANY run (unbounded history) with records > 0.

    Unlike ``has_historical_nonzero`` (which only inspects the in-window runs),
    this queries the full ``scraper_runs`` history. A scraper that captured for
    months and then broke, producing zeros for longer than ``lookback_days``,
    has all-zero in-window runs but is NOT never-captured — it is a long
    ``streak`` breach (#4611). One cheap ``LIMIT 1`` query, only for the small
    subset of scrapers with zero in-window captures.
    """
    with conn.cursor() as cur:
        cur.execute(EVER_CAPTURED_QUERY, (scraper_id,))
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

# Sentinel value for the ``known_scraper_ids`` parameter of
# ``check_zero_record_streaks``.  Using a private sentinel (instead of None)
# allows callers to distinguish "auto-load from registry" (default) from
# "explicitly disable the check" (pass None).


class _Sentinel:
    """Private sentinel type for the ``known_scraper_ids`` default."""


_SENTINEL: _Sentinel = _Sentinel()


def check_zero_record_streaks(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    *,
    now: datetime,
    threshold: int = DEFAULT_STREAK_THRESHOLD,
    daily_threshold: int = DEFAULT_DAILY_STREAK_THRESHOLD,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_age_days: int = DEFAULT_NEVER_CAPTURED_MIN_AGE_DAYS,
    min_runs: int = DEFAULT_NEVER_CAPTURED_MIN_RUNS,
    scraper_id: str | None = None,
    exclusions: set[str] | None = None,
    frequent_scraper_ids: set[str] | None = None,
    known_scraper_ids: set[str] | None | _Sentinel = _SENTINEL,
) -> CheckResult:
    """Run the zero-record streak check.

    Args:
        conn: Database connection (must see ``scraper_runs`` via the
            default search_path — i.e. ``public``/``staging`` paths set by
            the shared DATABASE_URL, which includes ``telemetry`` reads).
        now: Reference "current time" used for the lookback cutoff.
        threshold: Streak length that triggers a breach for frequent
            scrapers (runs more than once per day).
        daily_threshold: Streak length that triggers a breach for daily
            scrapers. Defaults to 2 — two consecutive missed daily runs
            is already ~48h of silent outage.
        lookback_days: How far back to consider runs. Runs older than
            this are ignored entirely.
        min_age_days: Minimum age (oldest run in window vs ``now``) before a
            never-captured scraper breaches. Younger scrapers stay
            ``insufficient_history``. See #4611.
        min_runs: Minimum run count before a never-captured scraper breaches.
            The applied run-count floor is ``max(applicable_threshold,
            min_runs)``.
        scraper_id: Optional single scraper ID to check.
        exclusions: Scrapers to skip. Defaults to ``EXCLUSIONS``.
        frequent_scraper_ids: Scraper IDs that run more than once per day.
            Defaults to ``FREQUENT_SCRAPER_IDS``.
        known_scraper_ids: Set of registered scraper IDs from
            ``framework.runner.get_scraper_ids()``.  When the sentinel is
            passed (the default), the function calls
            ``_load_known_scraper_ids()`` automatically.  Pass ``None``
            explicitly to disable the unknown-ID check (e.g. in environments
            without the venv).  Pass a set to override (useful in tests).

    Returns:
        ``CheckResult`` with breaches, insufficient-history entries, unknown
        scraper IDs, and counts for logging/summary output.
    """
    excl = exclusions if exclusions is not None else EXCLUSIONS
    freq = (
        frequent_scraper_ids
        if frequent_scraper_ids is not None
        else FREQUENT_SCRAPER_IDS
    )

    # Resolve known IDs: sentinel → auto-load; explicit None → skip check.
    if known_scraper_ids is _SENTINEL:
        known_scraper_ids = _load_known_scraper_ids()

    cutoff = now - timedelta(days=lookback_days)
    by_scraper = _fetch_recent_runs(conn, cutoff, scraper_id)

    breaches: list[Breach] = []
    insufficient: list[InsufficientHistory] = []
    unknown: list[str] = []
    checked = 0
    excluded = 0

    for sid in sorted(by_scraper.keys()):
        if sid in excl:
            excluded += 1
            continue

        # Partition: unknown scraper IDs are likely test pollution.
        if known_scraper_ids is not None and sid not in known_scraper_ids:
            unknown.append(sid)
            continue

        runs = by_scraper[sid]
        # Defensive: a scraper with runs older than cutoff only would have
        # been filtered out by the SQL WHERE clause, but if we somehow got
        # an empty list treat it as nothing to check.
        if not runs:
            continue

        checked += 1

        applicable_threshold = threshold if sid in freq else daily_threshold

        streak, first_zero, last_zero, last_nonzero = compute_streak(runs)

        if streak == 0:
            # Most recent run produced records — healthy.
            continue

        if not has_historical_nonzero(runs):
            # All runs in the lookback window are zero. This is EITHER a true
            # never-captured scraper OR a long streak whose last nonzero run
            # predates the window (e.g. 15 days of zeros with a 14d lookback).
            # ``has_historical_nonzero`` only inspects in-window rows, so we
            # must probe the unbounded history to tell them apart (#4611) —
            # otherwise a "started then stopped" scraper is mislabeled
            # never_captured, the exact distinction AC#3 exists to surface.
            run_count = len(runs)
            oldest_started_at = runs[-1][0]
            if oldest_started_at.tzinfo is None:
                oldest_started_at = oldest_started_at.replace(tzinfo=UTC)
            age = now - oldest_started_at

            if _ever_captured(conn, sid):
                # Long streak breach: last nonzero run is older than the
                # lookback window. Gate on the ordinary streak threshold (NOT
                # the never-captured run-count floor); too-few in-window runs
                # fall through to insufficient_history below.
                if run_count >= applicable_threshold:
                    breaches.append(
                        Breach(
                            scraper_id=sid,
                            streak=run_count,
                            threshold=applicable_threshold,
                            first_zero_at=_iso(first_zero),
                            last_zero_at=_iso(last_zero),
                            last_nonzero_at=None,
                            message=(
                                f"{sid}: at least {run_count} consecutive "
                                f"zero-record runs (threshold: "
                                f"{applicable_threshold}); last nonzero run is "
                                f"older than the {lookback_days}d lookback "
                                f"window"
                            ),
                            category="streak",
                        )
                    )
                    continue
            else:
                # True never-captured. Distinguish a genuinely new / barely-run
                # scraper (suppress as insufficient_history) from a registered,
                # scheduled, non-excluded scraper that has silently captured 0
                # records since deploy (breach). See #4611.
                run_count_floor = max(applicable_threshold, min_runs)
                if age >= timedelta(days=min_age_days) and run_count >= run_count_floor:
                    age_days = age.total_seconds() / 86400
                    breaches.append(
                        Breach(
                            scraper_id=sid,
                            streak=run_count,
                            threshold=run_count_floor,
                            first_zero_at=_iso(first_zero),
                            last_zero_at=_iso(last_zero),
                            last_nonzero_at=None,
                            message=(
                                f"{sid}: registered/scheduled but never captured "
                                f"since deploy — {run_count} zero-record runs over "
                                f"{age_days:.1f} days (run-count floor: "
                                f"{run_count_floor}, min age: {min_age_days}d)"
                            ),
                            category="never_captured",
                        )
                    )
                    continue

            # No breach fired for either sub-path (too young / too few runs for
            # never-captured; below applicable_threshold for the long streak).
            # Keep the brand-new false-positive guard intact.
            insufficient.append(
                InsufficientHistory(
                    scraper_id=sid,
                    zero_run_count=streak,
                    message=(
                        f"{sid}: {streak} zero-record runs but no historical "
                        f"nonzero run in the lookback window — insufficient "
                        f"history, not a breach"
                    ),
                )
            )
            continue

        if streak >= applicable_threshold:
            breaches.append(
                Breach(
                    scraper_id=sid,
                    streak=streak,
                    threshold=applicable_threshold,
                    first_zero_at=_iso(first_zero),
                    last_zero_at=_iso(last_zero),
                    last_nonzero_at=_iso(last_nonzero),
                    message=(
                        f"{sid}: {streak} consecutive zero-record runs "
                        f"(threshold: {applicable_threshold}, last nonzero "
                        f"run: {_iso(last_nonzero) or 'never in window'})"
                    ),
                )
            )

    if unknown:
        logger.warning(
            "WARNING: unknown scraper IDs in telemetry.scraper_runs "
            "(likely test pollution): %s",
            ", ".join(sorted(unknown)),
        )

    return CheckResult(
        breaches=breaches,
        insufficient_history=insufficient,
        checked_count=checked,
        excluded_count=excluded,
        unknown_scrapers=sorted(unknown),
    )


def _iso(value: datetime | None) -> str | None:
    """Format a datetime as ISO-8601 UTC string, or return None."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_json(result: CheckResult) -> str:
    """Machine-readable JSON summary."""
    payload = {
        "breaches": [asdict(b) for b in result.breaches],
        "insufficient_history": [asdict(ih) for ih in result.insufficient_history],
        "checked_count": result.checked_count,
        "excluded_count": result.excluded_count,
        "unknown_scrapers": result.unknown_scrapers,
        "healthy": len(result.breaches) == 0,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def format_text(result: CheckResult) -> str:
    """Human-readable summary."""
    lines: list[str] = []
    lines.append(
        f"Checked {result.checked_count} scraper(s), "
        f"{result.excluded_count} excluded, "
        f"{len(result.breaches)} breach(es), "
        f"{len(result.insufficient_history)} insufficient-history."
    )

    if result.unknown_scrapers:
        lines.append("")
        lines.append(
            "WARNING: unknown scraper IDs in telemetry.scraper_runs "
            "(likely test pollution):"
        )
        for uid in result.unknown_scrapers:
            lines.append(f"  * {uid}")

    if result.breaches:
        lines.append("")
        lines.append("BREACHES:")
        for b in result.breaches:
            label = "NEVER CAPTURED" if b.category == "never_captured" else "STREAK"
            lines.append(f"  * [{label}] {b.message}")
            lines.append(f"      first_zero_at={b.first_zero_at}")
            lines.append(f"      last_zero_at={b.last_zero_at}")
            lines.append(f"      last_nonzero_at={b.last_nonzero_at or '(none)'}")

    if result.insufficient_history:
        lines.append("")
        lines.append("INSUFFICIENT HISTORY (not a breach):")
        for ih in result.insufficient_history:
            lines.append(f"  * {ih.message}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect scrapers with N consecutive zero-record runs.",
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Machine-readable JSON output (default).",
    )
    out.add_argument(
        "--text",
        action="store_true",
        help="Human-readable text output.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_STREAK_THRESHOLD,
        help=(
            "Streak length that triggers a breach for frequent scrapers "
            f"(default: {DEFAULT_STREAK_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--daily-threshold",
        type=int,
        default=DEFAULT_DAILY_STREAK_THRESHOLD,
        help=(
            "Streak length that triggers a breach for daily scrapers "
            f"(default: {DEFAULT_DAILY_STREAK_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--scraper",
        type=str,
        default=None,
        help="Check only the specified scraper ID.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=(f"Runs older than this are ignored (default: {DEFAULT_LOOKBACK_DAYS})."),
    )
    parser.add_argument(
        "--never-captured-min-age-days",
        type=int,
        default=DEFAULT_NEVER_CAPTURED_MIN_AGE_DAYS,
        help=(
            "Minimum age before a never-captured scraper breaches "
            f"(default: {DEFAULT_NEVER_CAPTURED_MIN_AGE_DAYS})."
        ),
    )
    parser.add_argument(
        "--never-captured-min-runs",
        type=int,
        default=DEFAULT_NEVER_CAPTURED_MIN_RUNS,
        help=(
            "Minimum run count before a never-captured scraper breaches "
            f"(default: {DEFAULT_NEVER_CAPTURED_MIN_RUNS})."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.error(
            "DATABASE_URL is not set. Run via scripts/with-secret.sh or "
            "scripts/ecs-run-task.sh so the connection string is injected."
        )
        return 2

    with psycopg.connect(database_url, autocommit=True) as conn:
        result = check_zero_record_streaks(
            conn,
            now=datetime.now(UTC),
            threshold=args.threshold,
            daily_threshold=args.daily_threshold,
            lookback_days=args.lookback_days,
            min_age_days=args.never_captured_min_age_days,
            min_runs=args.never_captured_min_runs,
            scraper_id=args.scraper,
        )

    if args.text:
        print(format_text(result))
    else:
        print(format_json(result))

    return 0 if not result.breaches else 1


if __name__ == "__main__":
    raise SystemExit(main())
