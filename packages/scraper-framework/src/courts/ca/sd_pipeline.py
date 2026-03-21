"""San Diego Superior Court — Pipeline Scraper (Phase 1 -> Phase 2).

Chains the calendar scraper (Phase 1) with the tentative rulings scraper
(Phase 2) to fully automate San Diego tentative ruling collection:

1. Phase 1 (SDCalendarScraper): Fetches civil calendar pages, parses
   hearings, and filters for motion-type events to produce case numbers.
2. Phase 2 (SDTentativeRulingsScraper): For each case number, navigates
   the Odyssey ROA portal via Playwright to extract tentative rulings.

This scraper is the main entry point for San Diego tentative ruling
collection. It is registered in the runner as ``ca-sd-pipeline``.

Parent issue: #672
Integration issue: #903
"""

from __future__ import annotations

import structlog

from framework import BaseScraper, CapturedDocument, ScraperConfig
from framework.events import EventBus
from framework.storage import S3Archiver

logger = structlog.get_logger(__name__)


class SDPipelineScraper(BaseScraper):
    """Chains Phase 1 (calendar) and Phase 2 (ROA tentative rulings).

    Phase 1 enumerates motion-type hearings from the civil calendar and
    extracts case numbers. Phase 2 fetches tentative rulings for those
    case numbers from the Odyssey ROA portal.

    Parameters
    ----------
    config : ScraperConfig
        Pipeline scraper configuration.
    day_numbers : list[int] | None
        Which calendar day numbers (1-5) to fetch in Phase 1.
        Default: [2] (tomorrow's calendar).
    proxy_url : str | None
        Optional residential proxy URL for Phase 2's Cloudflare bypass.
    headless : bool
        Whether to run Phase 2's browser in headless mode. Default True.
    archiver : S3Archiver | None
        S3 archiver for raw content archival.
    event_bus : EventBus | None
        Event bus for health/document events.
    """

    def __init__(
        self,
        config: ScraperConfig,
        day_numbers: list[int] | None = None,
        proxy_url: str | None = None,
        headless: bool = True,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(config, archiver=archiver, event_bus=event_bus)
        self._day_numbers = day_numbers
        self._proxy_url = proxy_url
        self._headless = headless
        self._phase2_parser: BaseScraper | None = None

    def _create_phase1_scraper(
        self,
        archiver: S3Archiver | None,
        event_bus: EventBus | None,
    ) -> BaseScraper:
        """Create the Phase 1 calendar scraper.

        Each sub-scraper uses its own default config rather than inheriting
        the pipeline's config. This is intentional: Phase 1 (plain HTTP,
        1s delay, 3 retries) and Phase 2 (Playwright browser, 3s delay,
        2 retries) have fundamentally different optimal settings. Only
        ``s3_bucket`` is propagated since it is environment-level config,
        not scraper-tuning config.

        Separated into a method to allow test mocking.
        """
        from courts.ca.sd_calendar import SDCalendarScraper
        from courts.ca.sd_calendar import default_config as calendar_config

        config = calendar_config(s3_bucket=self.config.s3_bucket)
        return SDCalendarScraper(
            config=config,
            archiver=archiver,
            event_bus=event_bus,
            day_numbers=self._day_numbers,
        )

    def _create_phase2_scraper(
        self,
        case_numbers: list[str],
        archiver: S3Archiver | None,
        event_bus: EventBus | None,
    ) -> BaseScraper:
        """Create the Phase 2 tentative rulings scraper.

        Uses Phase 2's own default config — see ``_create_phase1_scraper``
        docstring for rationale on why sub-scraper configs are not inherited.

        Separated into a method to allow test mocking.
        """
        from courts.ca.sd_tentatives import SDTentativeRulingsScraper
        from courts.ca.sd_tentatives import default_config as tentatives_config

        config = tentatives_config(s3_bucket=self.config.s3_bucket)
        return SDTentativeRulingsScraper(
            config=config,
            case_numbers=case_numbers,
            proxy_url=self._proxy_url,
            headless=self._headless,
            archiver=archiver,
            event_bus=event_bus,
        )

    def fetch_documents(self) -> list[CapturedDocument]:
        """Run Phase 1 then Phase 2, returning tentative ruling documents.

        1. Run Phase 1 to enumerate motion-type calendar hearings.
        2. Extract and deduplicate case numbers from Phase 1 results.
        3. Run Phase 2 with those case numbers to fetch tentative rulings.
        4. Return Phase 2 documents.
        """
        # Phase 1: enumerate calendar hearings
        phase1 = self._create_phase1_scraper(
            archiver=self._archiver,
            event_bus=self._event_bus,
        )
        phase1_docs = phase1.fetch_documents()

        # Extract unique case numbers from motion hearings
        seen: set[str] = set()
        case_numbers: list[str] = []
        for doc in phase1_docs:
            cn = doc.case_number
            if cn and cn not in seen:
                seen.add(cn)
                case_numbers.append(cn)

        self._log.info(
            "Phase 1 complete",
            total_calendar_docs=len(phase1_docs),
            unique_case_numbers=len(case_numbers),
        )

        if not case_numbers:
            self._log.info("No case numbers from Phase 1 — skipping Phase 2")
            return []

        # Phase 2: fetch tentative rulings for discovered case numbers
        phase2 = self._create_phase2_scraper(
            case_numbers=case_numbers,
            archiver=self._archiver,
            event_bus=self._event_bus,
        )
        phase2_docs = phase2.fetch_documents()

        self._log.info(
            "Phase 2 complete",
            case_numbers_queried=len(case_numbers),
            rulings_found=len(phase2_docs),
        )

        return phase2_docs

    def _get_phase2_parser(self) -> BaseScraper:
        """Lazily create and cache a Phase 2 scraper instance for parsing.

        Reuses a single instance across all ``parse_document`` calls to
        avoid creating a new scraper object per document.
        """
        if self._phase2_parser is None:
            self._phase2_parser = self._create_phase2_scraper(
                case_numbers=[],
                archiver=self._archiver,
                event_bus=self._event_bus,
            )
        return self._phase2_parser

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse a Phase 2 document by delegating to SDTentativeRulingsScraper.

        This ensures parsing logic is defined in one place (Phase 2) and
        not duplicated in the pipeline orchestrator.
        """
        parser = self._get_phase2_parser()
        return parser.parse_document(doc)


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default San Diego pipeline scraper configuration.

    Uses the same schedule windows as the Phase 2 tentative rulings scraper
    since the pipeline's output is tentative rulings.
    """
    from datetime import time as dtime

    from framework import ScheduleWindow

    return ScraperConfig(
        scraper_id="ca-sd-pipeline",
        state="CA",
        county="San Diego",
        court="Superior Court",
        target_urls=[
            "https://www.sandiego.courts.ca.gov/portal/online/calendar",
            "https://odyroa.sdcourt.ca.gov",
        ],
        poll_interval_seconds=86400,  # daily
        schedule_windows=[
            ScheduleWindow(start=dtime(16, 15), end=dtime(17, 15)),  # 4:15 PM primary
            ScheduleWindow(start=dtime(2, 0), end=dtime(3, 0)),  # 2 AM catch-up
        ],
        request_delay_seconds=3.0,
        request_timeout_seconds=30.0,
        max_retries=2,
        s3_bucket=s3_bucket,
    )
