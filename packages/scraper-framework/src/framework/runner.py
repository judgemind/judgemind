"""Scraper runner — CLI entrypoint that runs scrapers with S3 archival and
Redis event bus wired in.

Reads JUDGEMIND_ARCHIVE_BUCKET from the environment. When set, every captured
document is archived to S3 via S3Archiver. When unset, scrapers run in
local-only mode (no archival) for development.

Reads REDIS_URL from the environment. When set, document.captured and
scraper.health events are emitted to Redis Streams. When unset, event
emission is silently skipped.

Usage:
    python -m framework.runner                  # run all registered scrapers
    python -m framework.runner ca-la-tentatives # run a single scraper by ID
"""

from __future__ import annotations

import os
import sys

import structlog

from .event_bus import RedisEventBus
from .models import ScraperConfig
from .storage import S3Archiver

logger = structlog.get_logger(__name__)

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

    from courts.ca.la_tentatives import LATentativeRulingsScraper
    from courts.ca.la_tentatives import default_config as la_config
    from courts.ca.oc_tentatives import OCTentativeRulingsScraper
    from courts.ca.oc_tentatives import default_config as oc_config
    from courts.ca.riverside_tentatives import RiversideTentativeRulingsScraper
    from courts.ca.riverside_tentatives import default_config as riverside_config
    from courts.ca.sb_tentatives import SBTentativeRulingsScraper
    from courts.ca.sb_tentatives import default_config as sb_config

    _REGISTRY.extend(
        [
            ("ca-la-tentatives-civil", LATentativeRulingsScraper, la_config),
            ("ca-oc-tentatives", OCTentativeRulingsScraper, oc_config),
            ("ca-riverside-tentatives", RiversideTentativeRulingsScraper, riverside_config),
            ("ca-sb-tentatives", SBTentativeRulingsScraper, sb_config),
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

    logger.info("Starting scraper run", scrapers=[e[0] for e in entries])

    had_failure = False

    for scraper_id, scraper_cls, config_factory in entries:
        log = logger.bind(scraper_id=scraper_id)
        log.info("Running scraper")

        config: ScraperConfig = config_factory(s3_bucket=bucket)
        scraper = scraper_cls(config=config, archiver=archiver, event_bus=event_bus)

        try:
            health = scraper.run()
        except Exception as exc:
            log.error("Unhandled exception in scraper", error=str(exc))
            had_failure = True
            continue

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
