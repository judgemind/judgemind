# Investigation: Contra Costa, Fresno, and Ventura Tentative Ruling Patterns

**Issue:** #155
**Date:** March 2026
**Status:** Complete

---

## Executive Summary

All three counties (Contra Costa, Fresno, Ventura) publish tentative rulings online and are feasible to scrape. Fresno is the easiest — it uses the same JCC/Drupal platform with PDF links per department, fitting directly into our existing PDF-link scraper template (Pattern 2). Contra Costa also publishes per-department PDFs but through a legacy ASP.NET page on a retired domain. Ventura is the most complex — it offers a custom .NET MVC search application that returns structured data (case number, motion type, department, document link) by hearing date, which is actually richer than most other county sources.

---

## Contra Costa County (Population Rank: 10)

### 1. Publication URL

The main court website is `https://contracosta.courts.ca.gov` (JCC/Drupal platform). The tentative rulings page at `/online-services/tentative-rulings` embeds an iframe pointing to a legacy ASP.NET application:

- **Iframe source:** `https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx`

This legacy page contains all the department listings and PDF links directly in the page HTML.

### 2. Pattern Classification

**Pattern 2 variant: Static PDF Links Per Department (ASP.NET legacy host)**

The structure is similar to Pattern 2 (OC/Riverside), but the index page is an ASP.NET page rather than Drupal, and PDFs are served from the legacy domain. Key difference: all PDF links are rendered server-side in a single HTML page — no JavaScript or AJAX loading required.

### 3. Update Frequency

- Civil tentative rulings available at **1:30 PM** on the court day preceding the hearing.
- Probate tentative rulings available **3-5 court days** before the hearing.
- Contest deadline: **4:00 PM** same day rulings are posted.

### 4. Format

**PDF files** hosted at `https://retired.cc-courts.org/civil/TR/Department NN - Judge Name/NN_MMDDYY.pdf`

URL pattern uses Windows-style backslash paths in the HTML (`TR\Department 09 - Judge Devine\09_122225.pdf`), which resolve to forward-slash URLs. The PDF filename pattern is `{dept_num}_{MMDDYY}.pdf`.

### 5. Departments (12 active)

| Dept | Judge | Type |
|------|-------|------|
| 09 | Judge Devine | Civil |
| 10 | Judge Campins | Civil |
| 14 | Judge Athanasiou | Civil |
| 16 | Judge Reyes | Civil |
| 18 | Judge Douglas | Civil |
| 20 | Judge OConnell | Civil |
| 30 | Judge George | Probate |
| 32 | Judge Hiramoto | Civil |
| 34 | Judge Leonard Marquez | Civil |
| 38 | Judge Hinton | Probate |
| 39 | Judge Weil | Civil |
| 57 | Comm Yamamoto | Civil |

Note: Departments are actively being reassigned (Dept 39 cases moving to Dept 10, Dept 18 to Dept 32, Dept 12 to Dept 39, Dept 9 to Dept 34 as of recent notices). The scraper must dynamically discover departments from the page rather than hardcoding them.

### 6. Reusability

**Moderate.** Cannot directly reuse `PdfLinkConfig` from the existing PDF-link scraper because:
- The index page is ASP.NET, not Drupal — different HTML structure
- PDF links are in a single long HTML line with `class='tentative-ruling'` anchors
- URLs use backslash paths and a legacy domain

However, the core flow (fetch index -> find PDF links -> download PDFs -> extract text) is identical. A CC-specific `PdfLinkConfig` with a custom `link_extractor` function could work with the existing template.

### 7. Difficulty: **Easy-Medium**

- PDF link discovery is straightforward (parse HTML for `class='tentative-ruling'` anchors)
- Judge names are embedded in the department headers
- Multiple weeks of historical rulings are kept on the page
- Main risk: the `retired.cc-courts.org` domain could be decommissioned at any time

### 8. Priority Recommendation: **P2 — Implement soon**

12 departments actively posting. The legacy domain adds some urgency — should implement before that domain goes away. Good candidate for the existing PDF-link scraper template with a custom config.

---

## Fresno County (Population Rank: 11)

### 1. Publication URL

- **Primary:** `https://www.fresno.courts.ca.gov/online-services/tentative-rulings`
- **NOTE:** The old domain `fresnosuperiorcourt.org` appears to be compromised (redirects to a gambling site). The correct domain is `fresno.courts.ca.gov` (JCC/Drupal platform).

### 2. Pattern Classification

**Pattern 2: Static PDF Links Per Department**

This is a textbook Pattern 2 implementation on the JCC/Drupal platform — identical structure to Riverside and San Bernardino.

### 3. Update Frequency

- Tentative rulings available at **3:00 PM** the day before the hearing.
- Oral argument must be requested by **4:00 PM** the day before.
- Phone line available (559) 457-4943 between 3:00 PM and 4:00 PM.

### 4. Format

**PDF files** hosted at `https://www.fresno.courts.ca.gov/system/files/tentative-rulings/{MM}-{DD}-{YY}-dept-{NNN}.pdf`

The URL pattern is highly regular and date-based. Some filenames have a `_0` suffix for amended versions (e.g., `03-04-26-dept-403_0.pdf`).

### 5. Departments (4 active)

| Dept | Phone |
|------|-------|
| 403 | (559) 457-6316 |
| 501 | (559) 457-6309 |
| 502 | (559) 457-6319 |
| 503 | (559) 457-6320 |

Note: Judge names are not listed on the tentative rulings page — they would need to be extracted from the PDF content or a separate judicial directory.

Each department keeps approximately 5 recent rulings linked on the page (about 1-2 weeks of history).

### 6. Reusability

**HIGH.** The page structure is virtually identical to Riverside and San Bernardino (same JCC/Drupal CMS with `/system/files/tentative-rulings/` PDF path pattern). The existing `PdfLinkConfig` + `PdfLinkScraper` template should work with minimal customization:
- Index URL: `https://www.fresno.courts.ca.gov/online-services/tentative-rulings`
- Link selector: `a[href*="/system/files/tentative-rulings/"]`
- Department extraction from filename: regex `dept-(\d+)` or from the `<h3>` headers

### 7. Difficulty: **Easy**

- Standard JCC/Drupal platform, same as existing scrapers
- Simple PDF link discovery
- 4 departments only — low volume
- PDF content parsing is the same as all other Pattern 2 courts

### 8. Priority Recommendation: **P2 — Implement soon, very low effort**

Should be one of the fastest counties to implement given the direct reuse of the existing PDF-link scraper template. Could be done in a single session.

---

## Ventura County (Population Rank: 12)

### 1. Publication URL

- **Primary:** `https://www2.ventura.courts.ca.gov/CaseInquiry/TentativeRulings`
- **Redirect from:** `https://ventura.courts.ca.gov/online-services/tentative-rulings-probate-notes` (JCC/Drupal site 301-redirects to the custom app on `www2` subdomain)

### 2. Pattern Classification

**New Pattern: Custom Search Application (ASP.NET MVC)**

This does not fit any of the 6 existing patterns cleanly. It is closest to Pattern 3 (Odyssey Portal) in that it requires a date-based search, but it is a custom application — not Tyler Odyssey. The key difference from Pattern 2 (PDF links): there is no index page listing all rulings. Instead, you must POST a search form with a hearing date to retrieve results.

### 3. Update Frequency

- Not explicitly stated on the page, but results are available by hearing date (standard tentative ruling timing — likely day before hearing).
- The search interface covers both civil tentative rulings and probate notes.

### 4. Format

**HTML search results table + individually downloadable documents**

The search returns a table with columns:
- **Case Number** (e.g., `2025CUOE051572`)
- **Event Type** (motion type, e.g., "Demurrer to Plaintiff's First Amended Complaint", "Motion to Set Aside/Vacate Default")
- **Event Date/Time** (e.g., "3/10/2026 8:20 AM")
- **Department** (e.g., 43, 20, 42)
- **Document** — link to view: `/CaseInquiry/ViewFile/{id}` (e.g., `/CaseInquiry/ViewFile/9040781`)

Each row represents a single ruling for a single motion. Documents are individually addressable via numeric IDs.

### 5. Data Richness

**This is the richest structured data source among the three counties.** A single date search for March 10, 2026 returned **50 results** (approximately 6 civil tentative rulings and 44 probate notes). The structured fields include:
- Case number
- Motion type (already parsed — no need for NLP extraction)
- Hearing date and time
- Department number
- Direct document link

Departments observed in sample: 20, 42, 43 (civil), plus probate departments.

### 6. Reusability

**LOW for existing templates, but the approach is reusable.** The search-and-download pattern would need a new scraper template:
1. GET the search page (establish session + get anti-forgery token)
2. POST with hearing date to get results table
3. Parse HTML table for case data
4. Download each document via `/CaseInquiry/ViewFile/{id}`
5. The anti-forgery token (`__RequestVerificationToken`) must be extracted from the form and included in each POST

This pattern could potentially be reused for other courts that have date-based search interfaces.

### 7. Difficulty: **Medium**

- Requires session management (cookies + anti-forgery tokens)
- Search-based rather than browse-based — must enumerate hearing dates
- Document download requires following individual links
- The structured data is a significant advantage — motion type and case number come pre-parsed
- The `www2` subdomain hosts a separate application from the Drupal site

### 8. Priority Recommendation: **P2 — Implement after Fresno/CC**

The rich structured data makes this valuable, but the custom search pattern requires more development effort. Implement after the easier Pattern 2 courts are done.

---

## Summary Table

| County | Pop. Rank | Pattern | Platform | URL | Depts | Difficulty | Priority |
|--------|-----------|---------|----------|-----|-------|------------|----------|
| **Contra Costa** | 10 | PDF Links (ASP.NET) | Legacy ASP.NET + JCC/Drupal | retired.cc-courts.org/civil/motions-hearings-tentative.aspx | 12 | Easy-Medium | P2 |
| **Fresno** | 11 | PDF Links (Drupal) | JCC/Drupal | fresno.courts.ca.gov/online-services/tentative-rulings | 4 | Easy | P2 |
| **Ventura** | 12 | Custom Search App | ASP.NET MVC | www2.ventura.courts.ca.gov/CaseInquiry/TentativeRulings | 3+ civil | Medium | P2 |

### Recommended Implementation Order

1. **Fresno** (Easy) — Direct reuse of PDF-link scraper template. Could be done in a single session.
2. **Contra Costa** (Easy-Medium) — PDF-link template with custom config for the ASP.NET legacy page.
3. **Ventura** (Medium) — New search-based scraper template. Richest data but most development effort.

---

## Additional Findings

### Fresno domain compromise
The old domain `fresnosuperiorcourt.org` has been compromised and redirects to a gambling site (`viptridewa.com`). The correct domain is `fresno.courts.ca.gov`. This should be noted in any documentation referencing Fresno court URLs.

### Contra Costa legacy domain risk
The `retired.cc-courts.org` domain name literally contains "retired" — this domain could be decommissioned at any time. The main court site (`contracosta.courts.ca.gov`) embeds it via iframe, suggesting the court is in a transition period. We should implement the scraper against the legacy URL but monitor for changes.

### Contra Costa Odyssey portal
Contra Costa also has an Odyssey portal at `odyportal.cc-courts.org/portal` (linked in the main site's Online Services menu). This could become relevant if tentative rulings migrate from the legacy ASP.NET page to the Odyssey portal.

### Ventura anti-scraping measures
The Ventura search page includes `<meta name="robots" content="noindex">` and `<meta name="googlebot" content="noindex">`, indicating the court does not want search engines to index the results. The anti-forgery token is required for each POST request. No CAPTCHA was observed.

---

## References

- `docs/specs/ca-county-investigation.md` — original pattern classification
- `packages/scraper-framework/src/courts/ca/pdf_link_scraper.py` — existing PDF-link template
- Issue #155 — this investigation's tracking issue
