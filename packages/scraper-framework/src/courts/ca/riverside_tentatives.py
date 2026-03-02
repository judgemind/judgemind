"""Riverside County Superior Court — Civil Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-02:
  URL:  https://www.riverside.courts.ca.gov/online-services/tentative-rulings
  17 PDF links found on index page (not all current — some from 2023/2024)
  IMPORTANT: site has an SSL certificate issue; verify_ssl=False required.

Link text format: "Department {CODE} - Honorable {Firstname Lastname [Suffix]}"
  e.g. "Department PS1 - Honorable Arthur Hester III"
  e.g. "Department M205 - Honorable Belinda Handy"

PDF URL pattern: /system/files/{YYYY-MM}/{CODE}ruling{MMDDYY}.pdf
  Resolved against BASE_URL.

PDF structure (PS1, 4 pages):
  Page 1: "Tentative Rulings for March 2, 2026\nDepartment PS1\n..."
  Case entries: "<N>.\n{CASE_NUMBER} {PARTY_VS_PARTY} {motion}\nTentative Ruling: ..."
  Case number format: CV + location code + year + seq, e.g. "CVPS2306157"

Courthouse mapping (best-effort — Riverside has many locations):
  PS*  → Palm Springs Courthouse
  M*   → Murrieta Courthouse (mid-county)
  MV*  → Moreno Valley Courthouse
  C*   → Corona Courthouse
  01–15 (numbered) → Hall of Justice (Riverside)
"""

from __future__ import annotations

import re
from typing import Any

from framework import ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

INDEX_URL = "https://www.riverside.courts.ca.gov/online-services/tentative-rulings"
BASE_URL = "https://www.riverside.courts.ca.gov"

# Link text: "Department PS1 - Honorable Arthur Hester III"
_LINK_TEXT_RE = re.compile(
    r"Department\s+(?P<department>\S+)\s*-\s*Honorable\s+(?P<judge_name>.+)",
    re.IGNORECASE,
)

# Case numbers like "CVPS2306157", "CVRI2412345"
_CASE_NUMBER_RE = re.compile(r"\bCV[A-Z]{2,4}\d{6,8}\b")


def _riv_courthouse(dept: str) -> str | None:
    dept_upper = dept.upper()
    if dept_upper.startswith("PS"):
        return "Palm Springs Courthouse"
    if dept_upper.startswith("MV"):
        return "Moreno Valley Courthouse"
    if dept_upper.startswith("M"):
        return "Murrieta Courthouse"
    if dept_upper.startswith("C"):
        return "Corona Courthouse"
    # Numbered departments (01–15): Hall of Justice in Riverside
    if dept_upper.isdigit() or dept_upper.lstrip("0").isdigit():
        return "Hall of Justice"
    return None


class RiversideTentativeRulingsScraper(PdfLinkScraper):
    """Riverside County civil tentative rulings — PDF-link pattern."""

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_LINK_TEXT_RE,
            courthouse_from_dept=_riv_courthouse,
            verify_ssl=False,  # Riverside has a bad TLS cert on the live site
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)


def default_config(s3_bucket: str = "") -> ScraperConfig:
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-riverside-tentatives-civil",
        state="CA",
        county="Riverside",
        court="Superior Court",
        target_urls=[INDEX_URL],
        poll_interval_seconds=43200,
        schedule_windows=[
            ScheduleWindow(start=dtime(15, 0), end=dtime(16, 0)),  # 3 PM sweep
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
