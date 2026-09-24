"""Contra Costa County Superior Court — Tentative Rulings Portal Scraper (Phase 1).

Scraper ID: ``ca-cc-tentatives-portal``. Runs concurrently with ``ca-cc-tentatives``
during the Phase 2 dual-run period (#2610). Daily coverage diff is produced by
``scripts/cc-dual-run-diff.py``.

This scraper targets the new Drupal-based portal at contracosta.courts.ca.gov,
running in parallel with the legacy ``cc_tentatives.py`` scraper (which targets
the retired ASP.NET site at retired.cc-courts.org).

It is **NOT a replacement** for ``cc_tentatives.py`` — both scrapers run
simultaneously during the dual-run phase (Phase 2 of migration plan #2601).
Cutover (Phase 3) and department-reassignment cleanup (Phase 4) are tracked
as separate follow-up issues under the parent #2601.

Portal discovery and migration plan: docs/investigations/contra-costa-portal-migration-2026-04.md

HTML structure (new portal):
  The /tentative-rulings listing page exposes a
  <select name="field_judge_target_id"> dropdown with known judge IDs.
  (The dropdown previously lived on a separate /test-page-tentative-rulings
  form page, but that page is now access-restricted and returns an HTTP 200
  access-denied body with no <select>, so judge discovery reads the live
  listing page instead — see #4591.)
  For each judge, GET /tentative-rulings?field_judge_target_id=<id> returns
  a Drupal Views table of rulings. Each row links to a detail page at
  /tentative-ruling/<slug>.

Listing table row format:
  <tr>
    <td><time datetime="2025-01-29T16:31:00">...</time></td>
    <td>
      <a href="/tentative-ruling/l24-04564">L24-04564</a>
      <p>SCOTT FUGERE VS. THE COUNTY OF CONTRA COSTA</p>
      Civil<p>CASE MANAGEMENT CONFERENCE</p>
    </td>
  </tr>

Detail page format (current portal, #4598):
  <article role="article" about="/tentative-ruling/l24-04564">
    <div class="jcc-body__main-text usa-prose clearfix">
      <h2>Case Number</h2><p><span>L24-04564</span></p>
      <h2>Case Type</h2><p><div>Civil</div></p>
      <h2>Hearing Date / Time</h2><p> Wed, 01/29/2025 - 08:31 </p>
      <h2>Nature of Proceedings</h2><p> CASE MANAGEMENT CONFERENCE </p>
      <h2>Tentative Ruling</h2>
      <p><p><a href="/system/files/general/16_012925.pdf">Tentative Ruling PDF</a></p>
         <p>Before the Court are ...</p></p>
    </div>
    <aside class="jcc-body__aside">
      <h4>BENJAMIN REYES</h4>
    </aside>
    <aside class="usa-footer">
      <a href="/system/files/traffic/tr-320-info.pdf">Traffic info</a>
    </aside>
  </article>

  The ruling content lives under the <h2>Tentative Ruling</h2> heading inside
  <div class="jcc-body__main-text">.  Every detail page also carries a
  boilerplate /system/files/traffic/ PDF (e.g. in a footer aside) that MUST be
  ignored — the real ruling PDF is the /system/files/general/ link inside the
  ruling section.  The parser falls back to the legacy
  <div class="field--name-body"> container for archived/older pages (which
  predate the jcc-body__main-text restructure).

Case number formats (matches the retired scraper):
  Civil:    C + 2-digit year + hyphen + 5 digits  (C24-02490)
  Limited:  L + 2-digit year + hyphen + 5 digits  (L23-06679)
  Probate:  N or P + 2-digit year + hyphen + 4-5 digits (N25-2307)
  Misc:     MSN + 2-digit year + hyphen + 4-5 digits (MSN23-2201)

Investigation: #2601
Phase 1 implementation: #2609
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from framework import BaseScraper, CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://contracosta.courts.ca.gov"
FORM_URL = f"{BASE_URL}/tentative-rulings"
LISTING_URL = f"{BASE_URL}/tentative-rulings"

# Case number pattern — matches civil, limited, probate, and misc formats.
# Anchored with ^ and $ so partial matches are rejected (e.g. "badnumber").
_CASE_NUMBER_RE = re.compile(r"^[CLNP]\d{2}-\d{4,5}$|^MSN\d{2}-\d{4,5}$")

# Test entry detection: slugs starting with "test" (case-insensitive).
_TEST_SLUG_RE = re.compile(r"^test", re.IGNORECASE)

# Extract department number from PDF filename pattern: "/16_012925.pdf" -> "16".
# Matches the retired-site filename convention preserved on the new portal.
_PDF_FILENAME_DEPT_RE = re.compile(r"/(\d{2})_\d{6}\.pdf$")


# ---------------------------------------------------------------------------
# Pure parsing helpers (module-level, testable without HTTP)
# ---------------------------------------------------------------------------


def _parse_judge_dropdown(html: str) -> list[tuple[str, str]]:
    """Parse the <select name="field_judge_target_id"> dropdown into (id, name) tuples.

    Args:
        html: Raw HTML of the form page.

    Returns:
        List of (value, text) tuples for each non-"All" option.
        Returns [] if the select element is missing.
    """
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", {"name": "field_judge_target_id"})
    if not select:
        return []

    results: list[tuple[str, str]] = []
    for option in select.find_all("option"):
        value = option.get("value", "")
        if not value or value.lower() == "all":
            continue
        text = option.get_text(strip=True)
        results.append((value, text))

    return results


def _parse_listing_table(html: str) -> list[dict]:
    """Parse the Drupal Views result table into a list of row dicts.

    Each dict has keys:
        slug (str): The slug from the detail-page href, e.g. "l24-04564".
        detail_url (str): Full absolute URL to the detail page.
        case_number (str | None): The case number text (link text).
        case_title (str | None): The case title from the first <p>.
        case_type (str | None): The case type text (between title and motion type).
        motion_type (str | None): The nature of proceedings from the second <p>.
        hearing_date (datetime | None): Parsed from <time datetime="...">.

    Returns:
        List of row dicts. Returns [] when the table is absent or empty.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    rows_data: list[dict] = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue

        # --- Column 0: hearing date ---
        time_tag = tds[0].find("time")
        hearing_date: datetime | None = None
        if time_tag:
            dt_str = time_tag.get("datetime", "")
            if dt_str:
                try:
                    # Parse ISO 8601 — strip trailing Z or timezone offset
                    dt_str_clean = dt_str.rstrip("Z")
                    # Handle with or without timezone suffix
                    for fmt in (
                        "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%dT%H:%M",
                    ):
                        try:
                            hearing_date = datetime.strptime(dt_str_clean, fmt).replace(tzinfo=UTC)
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass

        # --- Column 1: case info ---
        case_td = tds[1]

        # Slug and case number from link
        link = case_td.find("a")
        slug: str | None = None
        case_number: str | None = None
        detail_url: str = ""
        if link:
            href = link.get("href", "")
            if href:
                # Extract slug from href like "/tentative-ruling/l24-04564"
                slug = href.rstrip("/").rsplit("/", 1)[-1]
                detail_url = urljoin(BASE_URL, href)
            case_number = link.get_text(strip=True) or None

        if not slug:
            continue

        # Case title and motion type from <p> tags
        paragraphs = case_td.find_all("p")
        case_title: str | None = None
        motion_type: str | None = None
        if len(paragraphs) >= 1:
            case_title = paragraphs[0].get_text(strip=True) or None
        if len(paragraphs) >= 2:
            motion_type = paragraphs[1].get_text(strip=True) or None

        # Case type: text node(s) between the link and the first <p>
        # The structure is: <a>...</a><p>title</p>CaseType<p>motion</p>
        # We extract the text nodes directly from the td that are not inside
        # child elements, then find the type from text after the <p> for title.
        case_type: str | None = None
        # Walk td children to find text between paragraphs
        children_texts: list[str] = []
        found_first_p = False
        for child in case_td.children:
            tag_name = getattr(child, "name", None)
            if tag_name == "p":
                if not found_first_p:
                    found_first_p = True
                    continue
                else:
                    # This is the second <p> — stop
                    break
            elif found_first_p:
                # Text between first <p> and second <p>
                text = str(child).strip()
                if text:
                    children_texts.append(text)

        if children_texts:
            case_type = " ".join(children_texts).strip() or None

        rows_data.append(
            {
                "slug": slug,
                "detail_url": detail_url,
                "case_number": case_number,
                "case_title": case_title,
                "case_type": case_type,
                "motion_type": motion_type,
                "hearing_date": hearing_date,
            }
        )

    return rows_data


def _ruling_section_paragraphs(main_text: Tag) -> list[Tag]:
    """Collect the leaf <p> tags belonging to the Tentative Ruling section.

    The ``jcc-body__main-text`` div holds several <h2> sections (Case Number,
    Case Type, Hearing Date, Nature of Proceedings, Tentative Ruling).  Anchor
    on the <h2> whose text is "Tentative Ruling" and collect the leaf
    paragraphs that follow it (up to the next <h2>, if any), so the PDF link
    and ruling body are read from the ruling section only — never from Case
    Number etc., and never from the boilerplate traffic PDF that lives outside
    this container.

    Traversal uses ``find_all_next()`` (document order) rather than
    ``find_next_siblings()`` so the section boundary is detected at the next
    <h2> *regardless of nesting* — a future portal theming change that wraps a
    section in a styling <div> (``<h2>Tentative Ruling</h2><div>...<h2>Next
    Section</h2>...</div>``) still stops at that inner <h2> instead of leaking
    the following section's paragraphs into the ruling.  Candidates are scoped
    to descendants of ``main_text`` so the traversal never wanders into the
    sibling <aside>/footer that carries the boilerplate traffic PDF.
    """
    headings = main_text.find_all("h2")
    ruling_h2 = None
    for h2 in headings:
        if h2.get_text(strip=True).lower() == "tentative ruling":
            ruling_h2 = h2
            break
    if ruling_h2 is None:
        return []

    potential: list[Tag] = []
    for tag in ruling_h2.find_all_next():
        if not isinstance(tag, Tag):
            continue
        # Only consider nodes inside the jcc-body__main-text container; the
        # document-order traversal would otherwise reach the sibling
        # <aside>/footer that holds the boilerplate traffic PDF.
        if main_text not in tag.parents:
            continue
        # The next <h2> in document order ends the Tentative Ruling section,
        # whether it is a direct sibling or nested inside a wrapper <div>.
        if tag.name == "h2":
            break
        if tag.name == "p":
            potential.append(tag)

    # De-duplicate preserving order, then keep only leaf <p> (no nested <p>)
    # so wrapper paragraphs do not double-count text. lxml flattens invalid
    # nested <p><p>...</p></p> into siblings, so each real ruling paragraph is
    # already a leaf.
    unique = list(dict.fromkeys(potential))
    return [p for p in unique if not p.find("p")]


def _select_pdf_url(anchors: list[Tag]) -> str | None:
    """Pick the ruling PDF href from a list of <a> tags.

    Prefers a link under ``/system/files/general/`` (the real ruling PDF),
    falling back to the first ``.pdf`` link that is NOT under
    ``/system/files/traffic/`` (the boilerplate traffic PDF carried on every
    detail page).  Returns an absolute URL via ``urljoin``, or None.
    """
    fallback: str | None = None
    for a in anchors:
        href = a.get("href", "")
        if not href or ".pdf" not in href.lower():
            continue
        if "/system/files/general/" in href:
            return urljoin(BASE_URL, href)
        if "/system/files/traffic/" in href:
            continue
        if fallback is None:
            fallback = urljoin(BASE_URL, href)
    return fallback


def _leaf_paragraphs(container: Tag) -> list[Tag]:
    """Return only the innermost (leaf) <p> tags under a container element.

    Used for the legacy ``field--name-body`` <div>, whose ruling paragraphs
    are direct children.  ``find_all("p")`` returns the inner <p> descendants
    and does NOT include the ``container`` element itself.  BeautifulSoup/lxml
    auto-flattens the invalid nested ``<p><p>...</p></p>`` markup into sibling
    <p> tags, so a <p> never actually contains a nested <p> post-parse; the
    ``not p.find("p")`` guard simply keeps the predicate robust.
    """
    return [p for p in container.find_all("p") if not p.find("p")]


def _parse_detail_page(html: str) -> dict:
    """Extract structured fields from a detail page.

    Returns a dict with keys:
        ruling_text (str | None): Plain text of the ruling body.
        ruling_text_html (str | None): Raw HTML of the ruling body.
        pdf_url (str | None): Absolute URL to the linked PDF.
        judge_name (str | None): Judge name from the aside element.
    """
    soup = BeautifulSoup(html, "lxml")

    # --- PDF URL and ruling text from the ruling content ---
    ruling_text: str | None = None
    ruling_text_html: str | None = None
    pdf_url: str | None = None

    # Current portal: ruling content lives under
    # <div class="jcc-body__main-text"> after an <h2>Tentative Ruling</h2>
    # heading.  Archived/older pages used <div class="field--name-body">.
    main_text = soup.find("div", class_=lambda c: c and "jcc-body__main-text" in c)
    ruling_section: list = []
    body_field = None
    if main_text:
        ruling_section = _ruling_section_paragraphs(main_text)
    else:
        body_field = soup.find("div", class_=lambda c: c and "field--name-body" in c)
        if body_field:
            ruling_section = _leaf_paragraphs(body_field)

    if ruling_section:
        # PDF URL: prefer /system/files/general/, never /system/files/traffic/.
        anchors: list = []
        for p in ruling_section:
            anchors.extend(p.find_all("a"))
        pdf_url = _select_pdf_url(anchors)

        # Ruling body: leaf paragraphs whose only anchors are .pdf links are
        # the PDF-link paragraph and are excluded.
        text_parts: list[str] = []
        html_parts: list[str] = []
        for p in ruling_section:
            p_anchors = p.find_all("a")
            if p_anchors and all(".pdf" in a.get("href", "").lower() for a in p_anchors):
                continue
            text = p.get_text(separator=" ", strip=True)
            if text:
                text_parts.append(text)
                html_parts.append(str(p))

        if text_parts:
            ruling_text = "\n\n".join(text_parts)
        if html_parts:
            ruling_text_html = "\n".join(html_parts)

    # --- Judge name from aside ---
    judge_name: str | None = None
    aside = soup.find("aside", class_=lambda c: c and "jcc-body__aside" in c)
    if aside:
        h4 = aside.find("h4")
        if h4:
            judge_name = h4.get_text(strip=True) or None

    return {
        "ruling_text": ruling_text,
        "ruling_text_html": ruling_text_html,
        "pdf_url": pdf_url,
        "judge_name": judge_name,
    }


def _cc_dept_from_filename(pdf_url: str | None) -> str | None:
    """Extract department number from a PDF URL using the CC filename convention.

    The pattern is: /<dept>_<date>.pdf, e.g. "/system/files/general/16_012925.pdf"
    returns "16".

    Args:
        pdf_url: The PDF URL, or None.

    Returns:
        The department number string (e.g. "16"), or None if not parseable.
    """
    if not pdf_url:
        return None
    m = _PDF_FILENAME_DEPT_RE.search(pdf_url)
    if m:
        return m.group(1)
    return None


def _is_test_entry(slug: str, case_number: str | None) -> bool:
    """Return True if this listing row should be filtered out as a test entry.

    A row is a test entry if:
    - The slug matches ``^test`` (case-insensitive), OR
    - The case_number is None or does not match the valid CC case number regex.

    Args:
        slug: The slug extracted from the detail-page href.
        case_number: The case number text from the listing row link.

    Returns:
        True if the row should be skipped; False if it's a real case.
    """
    if _TEST_SLUG_RE.match(slug):
        return True
    if not case_number:
        return True
    if not _CASE_NUMBER_RE.match(case_number):
        return True
    return False


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------


class CCTentativesPortalScraper(BaseScraper):
    """Contra Costa County tentative rulings — new Drupal portal scraper (Phase 1).

    Runs in parallel with the legacy ``CCTentativeRulingsScraper`` (cc_tentatives.py)
    until Phase 3 cutover. See module docstring for the full migration plan.

    Fetch strategy:
    1. Fetch the form page to discover current judge IDs from the dropdown.
    2. For each judge, fetch the listing page (filtered by judge ID).
    3. For each listing row, fetch the detail page and download the linked PDF.
    4. Filter test entries (slug matches ^test, or case number invalid).
    5. Construct one CapturedDocument per valid ruling, with PDF as raw content.
    """

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch ruling documents from the new Contra Costa portal.

        Returns:
            List of CapturedDocument objects, one per valid ruling found.
            Returns [] on empty listings or missing dropdown; per-row
            exceptions are caught, logged, and skipped.
        """
        docs: list[CapturedDocument] = []

        with httpx.Client(
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
            follow_redirects=True,
            timeout=self.config.request_timeout_seconds,
        ) as client:
            # Step 1: Fetch the form page and parse judge IDs
            self._log.info("cc_portal.fetching_form", url=FORM_URL)
            try:
                form_response = client.get(FORM_URL)
                form_response.raise_for_status()
            except Exception as exc:
                self._log.error("cc_portal.form_fetch_failed", error=str(exc))
                return []

            judges = _parse_judge_dropdown(form_response.text)
            if not judges:
                self._log.error("cc_portal.no_judges_found", url=FORM_URL)
                return []

            self._log.info("cc_portal.judges_found", count=len(judges))

            # Step 2: Iterate each judge
            for judge_id, judge_name_dropdown in judges:
                time.sleep(self.config.request_delay_seconds)

                self._log.info(
                    "cc_portal.fetching_listing",
                    judge_id=judge_id,
                    judge_name=judge_name_dropdown,
                )
                try:
                    listing_response = client.get(
                        LISTING_URL,
                        params={"field_judge_target_id": judge_id},
                    )
                    listing_response.raise_for_status()
                except Exception as exc:
                    self._log.error(
                        "cc_portal.listing_fetch_failed",
                        judge_id=judge_id,
                        error=str(exc),
                    )
                    continue

                rows = _parse_listing_table(listing_response.text)
                if not rows:
                    self._log.info(
                        "cc_portal.empty_listing",
                        judge_id=judge_id,
                        judge_name=judge_name_dropdown,
                    )
                    continue

                self._log.info(
                    "cc_portal.listing_rows",
                    judge_id=judge_id,
                    count=len(rows),
                )

                # Step 3: Process each row
                for row in rows:
                    slug = row["slug"]
                    case_number = row["case_number"]

                    # Step 4: Filter test entries
                    if _is_test_entry(slug, case_number):
                        self._log.info(
                            "scraper.test_entry_skipped",
                            slug=slug,
                            case_number=case_number,
                            judge_id=judge_id,
                        )
                        continue

                    detail_url = row["detail_url"]
                    time.sleep(self.config.request_delay_seconds)

                    try:
                        doc = self._fetch_single_ruling(
                            client=client,
                            row=row,
                            judge_id=judge_id,
                            judge_name_dropdown=judge_name_dropdown,
                        )
                        if doc is not None:
                            docs.append(doc)
                    except Exception as exc:
                        self._log.error(
                            "cc_portal.row_fetch_failed",
                            slug=slug,
                            detail_url=detail_url,
                            error=str(exc),
                        )

        return docs

    def _fetch_single_ruling(
        self,
        client: httpx.Client,
        row: dict,
        judge_id: str,
        judge_name_dropdown: str,
    ) -> CapturedDocument | None:
        """Fetch the detail page and PDF for a single listing row.

        Builds a JSON envelope as ``raw_content`` so that
        ``parse_document`` can populate every structured field from
        ``raw_content`` alone — the reingest path
        (``scripts/reingest_from_s3.py::_reparse_document``) constructs a
        fresh ``CapturedDocument`` carrying only ``raw_content`` and DB
        identifiers, so anything not in the envelope is unrecoverable on
        reingest. See ``_populate_from_envelope`` for the field mapping
        and #4133 / #3986 for the refactor history.

        Args:
            client: The shared httpx client.
            row: Parsed row dict from _parse_listing_table.
            judge_id: The judge's field_judge_target_id value.
            judge_name_dropdown: The judge's display name from the dropdown.

        Returns:
            A populated CapturedDocument, or None if fetching fails.
        """
        detail_url = row["detail_url"]
        slug = row["slug"]

        # Fetch detail page
        detail_response = client.get(detail_url)
        detail_response.raise_for_status()
        detail_html_bytes = detail_response.content
        detail_html_text = detail_response.text

        detail = _parse_detail_page(detail_html_text)

        pdf_url = detail.get("pdf_url")
        if not pdf_url:
            self._log.warning("cc_portal.no_pdf_url", slug=slug, detail_url=detail_url)
            return None

        # Download PDF
        time.sleep(self.config.request_delay_seconds)
        pdf_response = client.get(pdf_url)
        pdf_response.raise_for_status()
        pdf_bytes = pdf_response.content

        # Build the JSON envelope (#4133 — Option A from the issue).  This
        # is the single source of truth for every structured field,
        # archived to S3 verbatim and round-tripped on reingest.  PDF and
        # detail HTML bytes are base64-encoded so the envelope is valid
        # UTF-8 / valid JSON; ``json.dumps(default=str)`` handles the
        # ``hearing_date`` datetime in the row dict.
        envelope = {
            "row": row,
            "detail_html_b64": base64.b64encode(detail_html_bytes).decode("ascii"),
            "pdf_url": pdf_url,
            "pdf_bytes_b64": base64.b64encode(pdf_bytes).decode("ascii"),
            "judge_id": judge_id,
            "judge_name_dropdown": judge_name_dropdown,
        }
        envelope_bytes = json.dumps(envelope, default=str).encode("utf-8")

        # ``ContentFormat.TEXT`` so the reingest text-extractor decodes
        # the envelope as UTF-8 (rather than handing it to pdfplumber as
        # if it were PDF bytes).  ``parse_document`` overwrites the
        # JSON-as-text ``ruling_text`` with the structured ruling text.
        doc = self._make_base_doc(
            source_url=detail_url,
            raw_content=envelope_bytes,
            content_format=ContentFormat.TEXT,
        )

        self._populate_from_envelope(doc, envelope=envelope)
        return doc

    def _populate_from_envelope(
        self,
        doc: CapturedDocument,
        *,
        envelope: dict[str, Any],
    ) -> None:
        """Populate structured fields on ``doc`` from a CC-portal JSON envelope.

        Single source of truth for the field-mapping logic shared by the
        live capture path (``_fetch_single_ruling``) and the reingest
        path (``parse_document``).  Mutates ``doc`` in place.  Mirrors
        the #3986 ``_populate_from_envelope`` shape used by the
        ``CourtListenerScraper``.

        The envelope shape is:

        ``{"row": <listing-row dict>, "detail_html_b64": <base64-HTML>,``
        ``"pdf_url": <str>, "pdf_bytes_b64": <base64-PDF>,``
        ``"judge_id": <str>, "judge_name_dropdown": <str>}``

        Args:
            doc: The document to populate.
            envelope: The decoded JSON envelope dict.
        """
        row = envelope.get("row") or {}
        if not isinstance(row, dict):
            row = {}

        pdf_url = envelope.get("pdf_url")
        judge_name_dropdown = envelope.get("judge_name_dropdown") or ""

        # Decode the detail HTML so we can re-derive ruling_text /
        # ruling_text_html / aside-judge_name without a network call.
        detail_html_b64 = envelope.get("detail_html_b64") or ""
        detail_html_bytes = b""
        detail_html_text = ""
        if detail_html_b64:
            try:
                detail_html_bytes = base64.b64decode(detail_html_b64)
                detail_html_text = detail_html_bytes.decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                detail_html_bytes = b""
                detail_html_text = ""
        detail = _parse_detail_page(detail_html_text) if detail_html_text else {}

        # --- Listing-row-derived fields ---
        doc.case_number = row.get("case_number")
        doc.case_title = row.get("case_title")
        doc.motion_type = row.get("motion_type")

        # ``hearing_date`` may be a datetime (live path) or an ISO-8601
        # string (reingest path — json.dumps(default=str) wrote it that
        # way).  Coerce to datetime so the schema gets a consistent type.
        hearing_date_raw = row.get("hearing_date")
        doc.hearing_date = _coerce_hearing_date(hearing_date_raw)

        # --- Detail-page-derived fields ---
        doc.ruling_text = detail.get("ruling_text")
        doc.ruling_text_html = detail.get("ruling_text_html")

        # Judge name: prefer aside (most specific), fall back to dropdown name.
        judge_name_aside = detail.get("judge_name")
        doc.judge_name = judge_name_aside or judge_name_dropdown or None

        # --- PDF-URL-derived fields ---
        # Department from PDF filename (16_012925.pdf -> "16").  Courthouse
        # from CC's per-department mapping (cc_tentatives._cc_courthouse).
        dept = _cc_dept_from_filename(pdf_url)
        if dept:
            doc.department = dept
            from courts.ca.cc_tentatives import _cc_courthouse

            courthouse = _cc_courthouse(dept)
            if courthouse:
                doc.courthouse = courthouse

        # --- Secondary artifacts in extra ---
        # Keep the same shape as the pre-#4133 version so downstream
        # consumers (S3 archival, the worker, tests) see no change.
        # ``detail_html`` stays in ``extra`` as bytes for backwards
        # compatibility with code that reads it (currently only tests).
        pdf_filename: str | None = None
        if pdf_url and "/" in pdf_url:
            pdf_filename = pdf_url.rsplit("/", 1)[-1]
        elif pdf_url:
            pdf_filename = pdf_url

        doc.extra["detail_html"] = detail_html_bytes
        doc.extra["detail_url"] = row.get("detail_url") or doc.source_url
        doc.extra["pdf_url"] = pdf_url
        doc.extra["pdf_filename"] = pdf_filename
        doc.extra["slug"] = row.get("slug")
        doc.extra["case_type"] = row.get("case_type")
        doc.extra["judge_id"] = envelope.get("judge_id")

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse structured fields from ``doc.raw_content``.

        Single source of truth for both the live capture path
        (``_fetch_single_ruling`` calls ``_populate_from_envelope`` after
        building the JSON envelope) and the reingest path
        (``scripts/reingest_from_s3.py::_reparse_document`` constructs a
        fresh ``CapturedDocument`` carrying only ``raw_content`` and
        calls this method).  Mirrors the #3986 fix shape on
        ``CourtListenerScraper.parse_document``.

        The raw_content is the JSON envelope built by
        ``_fetch_single_ruling`` (since #4133):

        ``{"row": <listing-row dict>, "detail_html_b64": <base64-HTML>,``
        ``"pdf_url": <str>, "pdf_bytes_b64": <base64-PDF>,``
        ``"judge_id": <str>, "judge_name_dropdown": <str>}``

        Tolerates ``raw_content`` that is not valid JSON or is missing
        the expected ``row`` key — pre-#4133 captures archived raw PDF
        bytes, so the envelope decode will fail.  In those cases the doc
        is returned unchanged so the reingest caller falls back to
        pdfplumber on the PDF bytes (via ``_extract_text_from_content``)
        and the DB-seeded ``case_number`` / ``case_title`` /
        ``hearing_date`` / ``judge_name`` / ``department`` from the
        rulings row (via the symmetric merge in #4142).

        Args:
            doc: The document to parse.

        Returns:
            The document with structured fields populated (envelope
            decode succeeded) or unchanged (envelope decode failed).
        """
        if not doc.raw_content:
            return doc

        try:
            payload = json.loads(doc.raw_content)
        except (ValueError, TypeError):
            # Not valid JSON — likely a pre-#4133 capture (raw PDF bytes).
            # Return unchanged so the reingest caller falls back to its
            # PDF-text-extract path plus DB-seeded fields.
            return doc

        if not isinstance(payload, dict):
            return doc

        if not isinstance(payload.get("row"), dict):
            # Envelope shape doesn't match what we wrote — leave the doc
            # untouched rather than risk populating with garbage.
            return doc

        self._populate_from_envelope(doc, envelope=payload)
        return doc


def _coerce_hearing_date(value: Any) -> datetime | None:
    """Coerce a hearing_date value (datetime, ISO string, or None) to datetime.

    The listing-row dict carries a real ``datetime`` on the live path,
    but ``json.dumps(default=str)`` serializes it to an ISO-8601 string
    that round-trips through reingest.  Accept either shape.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # ``str(datetime)`` produces "2025-01-29 16:31:00+00:00".
        # ``datetime.isoformat()`` produces "2025-01-29T16:31:00+00:00".
        # ``fromisoformat`` accepts both forms in Python 3.11+.
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
        # Fallback: try the older forms used by the live-path parser.
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(value, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default Contra Costa portal scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-cc-tentatives-portal",
        state="CA",
        county="Contra Costa",
        court="Superior Court",
        target_urls=[LISTING_URL],
        poll_interval_seconds=43200,  # twice daily (matches retired scraper)
        schedule_windows=[
            # Civil rulings posted by 1:30 PM day before hearing
            ScheduleWindow(start=dtime(14, 0), end=dtime(15, 0)),  # 2 PM sweep
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
