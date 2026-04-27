"""Scraper runner — CLI entrypoint that runs scrapers with S3 archival and
Redis event bus wired in.

Reads JUDGEMIND_ARCHIVE_BUCKET from the environment. When set, every captured
document is archived to S3 via S3Archiver. When unset, scrapers run in
local-only mode (no archival) for development.

Reads REDIS_URL from the environment. When set, document.captured and
scraper.health events are emitted to Redis Streams. When unset, event
emission is silently skipped.

Reads DATABASE_URL from the environment. When set, each scraper run is
recorded in the ``scraper_runs`` table. When unset, run recording is
silently skipped.

Usage:
    python -m framework.runner                  # run all registered scrapers
    python -m framework.runner ca-la-tentatives # run a single scraper by ID
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import structlog

from .event_bus import RedisEventBus
from .models import ScraperConfig, ScraperHealthEvent
from .s3_cache import make_s3_client
from .storage import S3Archiver

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Baselines path resolution (mirrors scripts/data-quality-check.py)
# ---------------------------------------------------------------------------

_RUNNER_DIR = Path(__file__).resolve().parent
# runner.py lives at packages/scraper-framework/src/framework/runner.py
# Repo root is four levels up.
_REPO_ROOT = _RUNNER_DIR.parent.parent.parent.parent
_DEFAULT_BASELINES_PATH = _REPO_ROOT / "data-quality-baselines.json"
_DOCKER_BASELINES_PATH = Path("/app/data-quality-baselines.json")


def _resolve_baselines_path() -> Path:
    """Return the best available baselines file path (mirrors data-quality-check.py)."""
    if _DEFAULT_BASELINES_PATH.exists():
        return _DEFAULT_BASELINES_PATH
    if _DOCKER_BASELINES_PATH.exists():
        return _DOCKER_BASELINES_PATH
    return _DEFAULT_BASELINES_PATH


def _load_scraper_schedules() -> dict[str, Any] | None:
    """Load the ``scraper_schedules`` block from data-quality-baselines.json.

    Returns the dict (may be empty) or None if the file is missing or the
    key is absent.  Never raises — missing baselines are treated as no
    overrides (all scrapers fire).
    """
    path = _resolve_baselines_path()
    try:
        with open(path) as f:
            data: dict[str, Any] = json.load(f)
        return data.get("scraper_schedules")  # None if key absent
    except Exception as exc:
        logger.warning(
            "scraper_schedules_load_error",
            path=str(path),
            error=str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# Cadence gating — _should_fire
# ---------------------------------------------------------------------------


def _should_fire(
    scraper_id: str,
    schedules: dict[str, Any] | None,
    now: datetime,
    sweep_interval_hours: float = 12,
) -> bool:
    """Return True if this scraper should run at *now* given the schedules config.

    Logic:
    - If schedules is None/empty or scraper_id is not in schedules → always fire
      (backwards-compatible default).
    - If a ``cron`` key is present and valid: fire only when the cron has had at
      least one scheduled instant in the half-open window (now - sweep_interval, now].
      The window is exclusive on the left (last sweep boundary) and inclusive on
      the right (now).
    - Malformed / missing / empty cron → log a warning and treat as no override
      (fire safely).

    Args:
        scraper_id: The scraper's registry ID.
        schedules: The ``scraper_schedules`` dict from data-quality-baselines.json,
            or None if the key is absent.
        now: The current datetime (must be timezone-aware).
        sweep_interval_hours: Width of the look-back window in hours (default 12,
            matching the EventBridge twice-daily cadence).

    Returns:
        True if the scraper should run, False if it should be skipped.
    """
    if not schedules:
        return True

    entry = schedules.get(scraper_id)
    if entry is None:
        return True

    cron_expr: str | None = entry.get("cron") if isinstance(entry, dict) else None
    if not cron_expr:
        return True

    try:
        from croniter import croniter  # type: ignore[import-untyped]

        window_start = now - timedelta(hours=sweep_interval_hours)
        # croniter.get_prev() returns the most recent fire time at or before ``now``.
        # We need a fire instant strictly after window_start and at or before now.
        itr = croniter(cron_expr, window_start)
        next_fire: datetime = itr.get_next(datetime)
        if next_fire <= now:
            return True
        return False
    except Exception as exc:
        logger.warning(
            "scraper_cadence_cron_parse_error",
            scraper_id=scraper_id,
            cron=cron_expr,
            error=str(exc),
        )
        return True


# ---------------------------------------------------------------------------
# Database helpers — scraper_runs recording
# ---------------------------------------------------------------------------


def _connect_db() -> psycopg.Connection | None:  # type: ignore[type-arg]
    """Open a DB connection from DATABASE_URL, or return None if unavailable."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return None
    try:
        conn = psycopg.connect(database_url, autocommit=True)
        return conn
    except Exception as exc:
        logger.warning("Failed to connect to database — run recording disabled", error=str(exc))
        return None


_ROSTER_DIRECTORIES = [
    ("courts.ca.oc_dept_judges", "OCCourtDirectory"),
    ("courts.ca.la_dept_judges", "LACourtDirectory"),
    ("courts.ca.fresno_dept_judges", "FresnoCourtDirectory"),
    ("courts.ca.kern_dept_judges", "KernCourtDirectory"),
    ("courts.ca.sd_dept_judges", "SanDiegoCourtDirectory"),
    ("courts.ca.sb_dept_judges", "SanBernardinoCourtDirectory"),
    ("courts.ca.ventura_dept_judges", "VenturaCourtDirectory"),
    ("courts.ca.sf_dept_judges", "SFCourtDirectory"),
    ("courts.ca.sc_tentatives", "SantaClaraCourtDirectory"),
]


def _fetch_rosters(
    s3_client: object,
    bucket: str,
    db_conn: psycopg.Connection,  # type: ignore[type-arg]
) -> None:
    """Fetch all court directory rosters before running scrapers."""
    import importlib

    for module_path, class_name in _ROSTER_DIRECTORIES:
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            directory = cls(s3_client=s3_client, s3_bucket=bucket, db_conn=db_conn)
            mapping = directory.fetch_and_snapshot(cls.COURT_ID)
            logger.info(
                "Roster fetched",
                court_id=cls.COURT_ID,
                departments=len(mapping) if mapping else 0,
            )
        except Exception as exc:
            logger.warning(
                "Roster fetch failed — continuing",
                court_id=class_name,
                error=str(exc),
            )
        time.sleep(1)


def _resolve_court_id(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    state: str,
    county: str,
    cache: dict[tuple[str, str], str | None],
) -> str | None:
    """Look up court UUID by state+county, with per-run caching."""
    key = (state, county)
    if key in cache:
        return cache[key]
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM courts WHERE state = %s AND county = %s LIMIT 1",
                (state, county),
            )
            row = cur.fetchone()
            court_id = str(row[0]) if row else None
    except Exception as exc:
        logger.warning("Failed to resolve court_id", state=state, county=county, error=str(exc))
        court_id = None
    cache[key] = court_id
    return court_id


def record_scraper_run(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    health: ScraperHealthEvent,
    config: ScraperConfig,
    court_id_cache: dict[tuple[str, str], str | None],
) -> None:
    """Write a row to scraper_runs for this scraper execution."""
    court_id = _resolve_court_id(conn, config.state, config.county, court_id_cache)
    started_at = health.run_timestamp
    completed_at = started_at + timedelta(seconds=health.response_time_seconds)
    status = "success" if health.success else "failure"
    response_time_ms = round(health.response_time_seconds * 1000)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scraper_runs
                    (scraper_id, court_id, started_at, completed_at, status,
                     records_captured, records_failed, error_message, response_time_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    health.scraper_id,
                    court_id,
                    started_at,
                    completed_at,
                    status,
                    health.records_captured,
                    0,  # records_failed not tracked by ScraperHealthEvent
                    health.error_message,
                    response_time_ms,
                ),
            )
        logger.info(
            "Recorded scraper run",
            scraper_id=health.scraper_id,
            status=status,
            records=health.records_captured,
        )
    except Exception as exc:
        logger.warning("Failed to record scraper run", scraper_id=health.scraper_id, error=str(exc))


def record_scraper_exception(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    scraper_id: str,
    config: ScraperConfig,
    started_at: datetime,
    error_message: str,
    elapsed_seconds: float,
    court_id_cache: dict[tuple[str, str], str | None],
) -> None:
    """Record a scraper_runs row for a scraper that raised an unhandled exception."""
    court_id = _resolve_court_id(conn, config.state, config.county, court_id_cache)
    completed_at = started_at + timedelta(seconds=elapsed_seconds)
    response_time_ms = round(elapsed_seconds * 1000)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scraper_runs
                    (scraper_id, court_id, started_at, completed_at, status,
                     records_captured, records_failed, error_message, response_time_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    scraper_id,
                    court_id,
                    started_at,
                    completed_at,
                    "failure",
                    0,
                    0,
                    error_message,
                    response_time_ms,
                ),
            )
        logger.info(
            "Recorded scraper exception run",
            scraper_id=scraper_id,
            error=error_message,
        )
    except Exception as exc:
        logger.warning(
            "Failed to record scraper exception run", scraper_id=scraper_id, error=str(exc)
        )


# ---------------------------------------------------------------------------
# Scraper registry
# ---------------------------------------------------------------------------

# Each entry: (scraper_id, config_factory, scraper_class)
# Config factories accept an optional s3_bucket kwarg.
_REGISTRY: list[tuple[str, type, callable]] = []


def _build_registry() -> list[tuple[str, type, callable]]:
    """Lazily discover all known scrapers. Imports are deferred so the module
    stays importable even when court-specific dependencies are missing."""
    if _REGISTRY:
        return _REGISTRY

    from courts.ca.cc_tentatives import CCTentativeRulingsScraper
    from courts.ca.cc_tentatives import default_config as cc_config
    from courts.ca.cc_tentatives_portal import CCTentativesPortalScraper
    from courts.ca.cc_tentatives_portal import default_config as cc_portal_config
    from courts.ca.fresno_tentatives import FresnoTentativeRulingsScraper
    from courts.ca.fresno_tentatives import default_config as fresno_config
    from courts.ca.governor_appointments import GovernorAppointmentsScraper
    from courts.ca.governor_appointments import default_config as gov_appt_config
    from courts.ca.la_tentatives import (
        LAAppellateTentativeRulingsScraper,
        LATentativeRulingsScraper,
    )
    from courts.ca.la_tentatives import default_config as la_config
    from courts.ca.la_tentatives import default_config_appellate as la_appellate_config
    from courts.ca.oc_family_law_tentatives import OCFamilyLawTentativeRulingsScraper
    from courts.ca.oc_family_law_tentatives import default_config as oc_fl_config
    from courts.ca.oc_probate_tentatives import OCProbateTentativeRulingsScraper
    from courts.ca.oc_probate_tentatives import default_config as oc_probate_config
    from courts.ca.oc_tentatives import OCTentativeRulingsScraper
    from courts.ca.oc_tentatives import default_config as oc_config
    from courts.ca.riverside_tentatives import RiversideTentativeRulingsScraper
    from courts.ca.riverside_tentatives import default_config as riverside_config
    from courts.ca.sb_tentatives import SBTentativeRulingsScraper
    from courts.ca.sb_tentatives import default_config as sb_config
    from courts.ca.sc_tentatives import SCTentativeRulingsScraper
    from courts.ca.sc_tentatives import default_config as sc_config
    from courts.ca.sd_calendar import SDCalendarScraper
    from courts.ca.sd_calendar import default_config as sd_cal_config
    from courts.ca.sd_pipeline import SDPipelineScraper
    from courts.ca.sd_pipeline import default_config as sd_pipeline_config
    from courts.ca.sd_tentatives import SDTentativeRulingsScraper
    from courts.ca.sd_tentatives import default_config as sd_config
    from courts.ca.sf_civil_tentatives import SFCivilTentativeRulingsScraper
    from courts.ca.sf_civil_tentatives import default_config as sf_civil_config
    from courts.ca.sf_tentatives import SFTentativeRulingsScraper
    from courts.ca.sf_tentatives import default_config as sf_config
    from courts.ca.ventura_tentatives import VenturaTentativeRulingsScraper
    from courts.ca.ventura_tentatives import default_config as ventura_config
    from courts.federal.courtlistener import CourtListenerScraper
    from courts.federal.courtlistener import default_config as cl_config

    _REGISTRY.extend(
        [
            ("ca-cc-tentatives", CCTentativeRulingsScraper, cc_config),
            ("ca-cc-tentatives-portal", CCTentativesPortalScraper, cc_portal_config),
            ("ca-fresno-tentatives-civil", FresnoTentativeRulingsScraper, fresno_config),
            ("ca-governor-appointments", GovernorAppointmentsScraper, gov_appt_config),
            ("ca-la-tentatives-civil", LATentativeRulingsScraper, la_config),
            (
                "ca-la-tentatives-appellate",
                LAAppellateTentativeRulingsScraper,
                la_appellate_config,
            ),
            ("ca-oc-tentatives", OCTentativeRulingsScraper, oc_config),
            ("ca-oc-tentatives-family-law", OCFamilyLawTentativeRulingsScraper, oc_fl_config),
            ("ca-oc-tentatives-probate", OCProbateTentativeRulingsScraper, oc_probate_config),
            ("ca-riverside-tentatives", RiversideTentativeRulingsScraper, riverside_config),
            ("ca-sb-tentatives", SBTentativeRulingsScraper, sb_config),
            ("ca-sc-tentatives", SCTentativeRulingsScraper, sc_config),
            ("ca-sd-calendar", SDCalendarScraper, sd_cal_config),
            ("ca-sd-pipeline", SDPipelineScraper, sd_pipeline_config),
            ("ca-sd-tentatives", SDTentativeRulingsScraper, sd_config),
            ("ca-sf-tentatives-civil", SFCivilTentativeRulingsScraper, sf_civil_config),
            ("ca-sf-tentatives-family-law", SFTentativeRulingsScraper, sf_config),
            ("ca-ventura-tentatives", VenturaTentativeRulingsScraper, ventura_config),
            ("federal-courtlistener-opinions", CourtListenerScraper, cl_config),
        ]
    )
    return _REGISTRY


def get_scraper_ids() -> list[str]:
    """Return all registered scraper IDs."""
    return [entry[0] for entry in _build_registry()]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_scrapers(scraper_ids: list[str] | None = None) -> int:
    """Run the specified scrapers (or all if none specified).

    Returns 0 on success, 1 if any scraper run raised an unhandled exception.
    """
    bucket = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "")
    archiver: S3Archiver | None = None

    if bucket:
        archiver = S3Archiver(bucket=bucket)
        logger.info("S3 archival enabled", bucket=bucket)
    else:
        logger.info("S3 archival disabled (JUDGEMIND_ARCHIVE_BUCKET not set)")

    event_bus = RedisEventBus.from_env()

    db_conn = _connect_db()
    if db_conn:
        logger.info("Database recording enabled")
    else:
        logger.info("Database recording disabled (DATABASE_URL not set or connection failed)")

    # Fetch court directory rosters before running scrapers.
    # Fresh dept→judge mappings ensure accurate judge assignment.
    if db_conn and bucket:
        _fetch_rosters(make_s3_client(), bucket, db_conn)

    try:
        court_id_cache: dict[tuple[str, str], str | None] = {}

        registry = _build_registry()

        # Filter to requested scrapers
        if scraper_ids:
            known_ids = {entry[0] for entry in registry}
            unknown = set(scraper_ids) - known_ids
            if unknown:
                logger.error("Unknown scraper IDs", ids=sorted(unknown), known=sorted(known_ids))
                return 1
            entries = [entry for entry in registry if entry[0] in scraper_ids]
        else:
            entries = list(registry)

        # Apply cadence gating: skip scrapers whose cron override has not
        # fired within the current sweep window.  Read schedules once from
        # data-quality-baselines.json (same loader path as the alerter).
        scraper_schedules = _load_scraper_schedules()
        if scraper_schedules is not None:
            now_utc = datetime.now(UTC)
            gated_entries = []
            for entry in entries:
                sid = entry[0]
                if _should_fire(sid, scraper_schedules, now_utc):
                    gated_entries.append(entry)
                else:
                    logger.info(
                        "scraper_skipped_by_cadence",
                        scraper_id=sid,
                        cron=scraper_schedules.get(sid, {}).get("cron"),
                        now=now_utc.isoformat(),
                    )
            entries = gated_entries

        logger.info("Starting scraper run", scrapers=[e[0] for e in entries])

        had_failure = False

        # Pre-fetch the LA department-to-judge mapping if we're running the LA scraper.
        # This is done once per run and shared across all LA scraper instances.
        la_dept_judge_map: dict[str, str] = {}
        la_scraper_ids = {"ca-la-tentatives-civil"}
        if any(e[0] in la_scraper_ids for e in entries):
            try:
                from courts.ca.la_dept_judges import fetch_department_judge_mapping

                la_dept_judge_map = fetch_department_judge_mapping()
                logger.info(
                    "Loaded LA dept-judge mapping",
                    departments=len(la_dept_judge_map),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch LA dept-judge mapping — judge names from "
                    "department lookup will be unavailable this run",
                    error=str(exc),
                )

        # Pre-fetch the Riverside department-to-judge mapping (#585).
        riv_dept_judge_map: dict[str, str] = {}
        riv_scraper_ids = {"ca-riverside-tentatives"}
        if any(e[0] in riv_scraper_ids for e in entries):
            try:
                from courts.ca.riverside_dept_judges import (
                    fetch_department_judge_mapping as fetch_riv_mapping,
                )

                riv_dept_judge_map = fetch_riv_mapping()
                logger.info(
                    "Loaded Riverside dept-judge mapping",
                    departments=len(riv_dept_judge_map),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch Riverside dept-judge mapping — judge names from "
                    "department lookup will be unavailable this run",
                    error=str(exc),
                )

        # Pre-fetch the Ventura department-to-judge mapping (#1175).
        ventura_dept_judge_map: dict[str, str] = {}
        ventura_scraper_ids = {"ca-ventura-tentatives"}
        if any(e[0] in ventura_scraper_ids for e in entries):
            try:
                from courts.ca.ventura_dept_judges import (
                    fetch_department_judge_mapping as fetch_ventura_mapping,
                )

                ventura_dept_judge_map = fetch_ventura_mapping()
                logger.info(
                    "Loaded Ventura dept-judge mapping",
                    departments=len(ventura_dept_judge_map),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch Ventura dept-judge mapping — judge names from "
                    "department lookup will be unavailable this run",
                    error=str(exc),
                )

        # Pre-fetch the SF department-to-judge mapping for the civil scraper.
        sf_dept_judge_map: dict[str, str] = {}
        sf_civil_scraper_ids = {"ca-sf-tentatives-civil"}
        if any(e[0] in sf_civil_scraper_ids for e in entries):
            try:
                from courts.ca.sf_dept_judges import (
                    fetch_department_judge_mapping as fetch_sf_mapping,
                )

                sf_dept_judge_map = fetch_sf_mapping()
                logger.info(
                    "Loaded SF dept-judge mapping",
                    departments=len(sf_dept_judge_map),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch SF dept-judge mapping — judge names from "
                    "department lookup will be unavailable this run",
                    error=str(exc),
                )

        for scraper_id, scraper_cls, config_factory in entries:
            log = logger.bind(scraper_id=scraper_id)
            log.info("Running scraper")

            config: ScraperConfig = config_factory(s3_bucket=bucket)

            # Pass dept-judge mapping to LA, Riverside, Ventura, and SF scrapers
            extra_kwargs: dict[str, object] = {}
            if scraper_id in la_scraper_ids and la_dept_judge_map:
                extra_kwargs["dept_judge_map"] = la_dept_judge_map
            if scraper_id in riv_scraper_ids and riv_dept_judge_map:
                extra_kwargs["dept_judge_map"] = riv_dept_judge_map
            if scraper_id in ventura_scraper_ids and ventura_dept_judge_map:
                extra_kwargs["dept_judge_map"] = ventura_dept_judge_map
            if scraper_id in sf_civil_scraper_ids and sf_dept_judge_map:
                extra_kwargs["dept_judge_map"] = sf_dept_judge_map

            run_started_at = datetime.now(UTC)
            run_start = time.monotonic()

            try:
                scraper = scraper_cls(
                    config=config,
                    archiver=archiver,
                    event_bus=event_bus,
                    db_conn=db_conn,
                    **extra_kwargs,
                )
            except Exception as exc:
                log.error("Scraper construction failed", error=str(exc))
                if db_conn:
                    record_scraper_exception(
                        db_conn,
                        scraper_id,
                        config,
                        run_started_at,
                        str(exc),
                        time.monotonic() - run_start,
                        court_id_cache,
                    )
                had_failure = True
                continue

            try:
                health = scraper.run()
            except Exception as exc:
                log.error("Unhandled exception in scraper", error=str(exc))
                elapsed = time.monotonic() - run_start
                if db_conn:
                    record_scraper_exception(
                        db_conn,
                        scraper_id,
                        config,
                        run_started_at,
                        str(exc),
                        elapsed,
                        court_id_cache,
                    )
                had_failure = True
                continue

            if db_conn:
                record_scraper_run(db_conn, health, config, court_id_cache)

            if health.success:
                log.info(
                    "Scraper completed",
                    records=health.records_captured,
                    time_seconds=round(health.response_time_seconds, 2),
                )
            else:
                log.error(
                    "Scraper reported failure",
                    error=health.error_message,
                    records=health.records_captured,
                )
                had_failure = True
    finally:
        if db_conn:
            try:
                db_conn.close()
            except Exception:
                pass

    if not had_failure:
        # This marker is matched by the CloudWatch metric filter
        # (Judgemind/Scraper ScraperSuccessCount). If this log line does not
        # appear within 24 hours the "no-success" alarm fires.
        logger.info("scraper_run_complete", scrapers=[e[0] for e in entries])

    return 1 if had_failure else 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint: ``python -m framework.runner [scraper_id ...]``."""
    scraper_ids = sys.argv[1:] if len(sys.argv) > 1 else None
    exit_code = run_scrapers(scraper_ids)
    sys.exit(exit_code)
