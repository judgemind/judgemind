# California County Tentative Rulings: Multi-County Investigation

**Judgemind Project — Scraper Architecture Research**
**Date: March 2026**

---

## Executive Summary

We investigated tentative ruling publication methods across California's major counties to identify shared patterns before writing scraper code. The key finding: **there is no single dominant publication method for tentative rulings**. Despite Tyler Odyssey being the backend CMS for 28+ of 58 counties, courts publish tentative rulings through at least six distinct mechanisms. However, several patterns repeat across multiple counties, which means a well-designed scraper framework can use parameterized templates to cover many courts efficiently.

**Bottom line for scraper architecture:** Design for 5–6 scraper "types" (templates), each parameterized per court. One LA-style scraper won't work for Orange County. But one PDF-link-scraper template, properly parameterized, could cover Orange, Riverside, San Bernardino, and potentially a dozen smaller counties.

---

## Pattern Classification

### Pattern 1: ASP.NET Dynamic Form (LA County)

**Counties:** Los Angeles

**How it works:** Custom ASP.NET WebForms page with dropdown menus listing courthouse/department/date combinations. User selects an option, form POSTs with `__VIEWSTATE` and `__EVENTVALIDATION` tokens, server returns ruling HTML.

**Scraping approach:** Enumerate dropdown options daily, POST for each combination, parse returned HTML. Simple HTTP requests sufficient (server-rendered, no JavaScript required).

**URL:** `https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil`

**Key details:**
- ~100 department/date combinations on a typical day
- Unchanged since 2016
- No CAPTCHA on civil tentatives
- Primary scrape at 6 PM, catch-up at 2 AM
- Each department formats rulings differently — parser needs per-department flexibility

**Reusability:** Low. This is a one-off implementation specific to LA's custom system. However, LA is by far the highest-volume single court, so the investment is justified.

---

### Pattern 2: Static PDF Links Per Judge (Orange County, Riverside, San Bernardino)

**Counties:** Orange, Riverside, San Bernardino, and likely many smaller counties

**How it works:** Court website has a page listing judges with direct links to PDF files containing their tentative rulings. Each judge has one PDF that gets overwritten with each new batch of rulings. PDFs are typically posted by 3:00 PM the day before the hearing.

**Scraping approach:** Scrape the index page daily to get current PDF URLs, download each PDF, extract text, parse rulings from the PDF content. Need PDF text extraction (pdftotext or similar).

**Orange County specifics:**
- URL: `https://www.occourts.org/online-services/tentative-rulings/civil-tentative-rulings`
- Hosted on Pantheon CMS (Drupal-based)
- ~35 judges with active civil tentative ruling PDFs
- Organized by courthouse panel: Complex Civil (CX101–CX105), Central Justice Center, North Justice Center, West Justice Center, Costa Mesa Complex
- PDF URL pattern: `/sites/default/files/oc/default/tentative-rulings/{judgename}rulings.pdf`
- Also has Family Law and Probate tentative rulings on separate pages
- PDFs are overwritten (not archived) — must capture daily before replacement

**Riverside County specifics:**
- URL: `https://www.riverside.courts.ca.gov/online-services/tentative-rulings`
- PDF URL pattern: `/system/files/{date-path}/{DeptCode}ruling{date}.pdf`
- Departments across Riverside and desert locations
- Posted by 3:00 PM day before hearing
- Also uses Pantheon/Drupal-based CMS

**San Bernardino County specifics:**
- URL: `https://sanbernardino.courts.ca.gov/online-services/civil-tentative-rulings`
- Started posting tentative rulings online in February 2021 (relatively recent)
- Posted after 3:00 PM and 7:00 PM (two posting windows)
- Departments across San Bernardino (S-prefix), Rancho Cucamonga (R-prefix), Victorville (V-prefix)

**Reusability:** HIGH. One PDF-link-scraper template parameterized with:
- Index page URL
- CSS selector or pattern for extracting judge/PDF links
- PDF URL pattern
- Judge name extraction regex
- Courthouse/department mapping

This template could potentially cover 10–15+ counties with minimal per-county customization.

---

### Pattern 3: Tyler Odyssey Public Access Portal (San Diego, Sacramento-adjacent)

**Counties:** San Diego (confirmed), and potentially many others using Odyssey for case management

**How it works:** Tentative rulings are accessible through the court's Odyssey-powered "Register of Actions" or public portal. Users look up a case and can view the tentative ruling attached to it. This is case-lookup-based rather than browse-based — you need to know the case number or search by hearing date.

**San Diego specifics:**
- URL: `https://odyroa.sdcourt.ca.gov/`
- Free to view tentative rulings (no cost)
- Available by 4:00 PM day before hearing
- Tentative rulings integrated into case record (not separate page)
- This is harder to scrape comprehensively because there's no single "browse all today's tentatives" page — requires knowing which cases have hearings

**Scraping approach:** Two options:
1. If the portal has a calendar/hearing search, enumerate hearings by date and retrieve tentatives per case
2. If not, need to combine with court calendar data to identify which cases have upcoming hearings, then look up each case

**Reusability:** MODERATE. Tyler Odyssey portals share a common URL pattern (`odyroa.{domain}`) and similar UI. But the public access configuration varies by court — some expose more search/browse functionality than others.

**Important note:** Tyler Odyssey is the backend CMS for 28+ of 58 CA counties (~70% of population), but this does NOT mean all those counties publish tentatives through the Odyssey portal. Many Odyssey courts still publish tentatives separately via PDFs or court website pages. The CMS and the tentative ruling publication method are often decoupled.

---

### Pattern 4: Custom Web Application (San Francisco)

**Counties:** San Francisco

**How it works:** Custom DLL-based web application (`webapps.sftc.org/tr/tr.dll`) with separate endpoints per department/ruling type. Each link returns the current tentative rulings for that department.

**SF specifics:**
- URL: `https://sf.courts.ca.gov/online-services/tentative-rulings`
- Individual links per department/topic:
  - Asbestos Discovery, Law & Motion, Motion Calendar (Dept 304)
  - Law & Motion/Discovery (Depts 301, 302)
  - Probate (with CAPTCHA gateway)
  - Real Property Housing Court (Depts 210, 501)
  - Family Law (separate app: `webapps.sftc.org/ufctr/ufctr.dll`)
- DLL-based endpoint: `http://webapps.sftc.org/tr/tr.dll/?RulingID={N}`
- Probate has a CAPTCHA: `webapps.sftc.org/captcha/captcha.dll`
- Limited number of departments posting tentatives (~7 civil endpoints)

**Scraping approach:** Hit each endpoint URL daily, parse the HTML response. Simple GET requests likely sufficient (the DLL endpoints appear to be direct content delivery, not form-based).

**Reusability:** LOW. Custom to SF. But SF is a relatively small number of endpoints, so the implementation cost is low.

---

### Pattern 5: Per-Department Web Pages with Document Links (Santa Clara)

**Counties:** Santa Clara, and likely several mid-size counties

**How it works:** The court website has a tentative rulings landing page that links to individual department pages. Each department page has a link to the current ruling document (often a Word doc, PDF, or HTML page that gets replaced weekly).

**Santa Clara specifics:**
- URL: `https://santaclara.courts.ca.gov/online-services/tentative-rulings`
- Individual pages per department (Depts 1, 2, 6, 10, 16, 19, 22)
- Rulings posted after 2:00 PM day before hearing
- Rulings "remain accessible until replaced by successive rulings" — ephemeral
- Links point to downloadable files (typically PDF or DOCX)
- Only ~7 departments currently posting online
- Court is transitioning from Microsoft Teams to Unicorn Digital Courtroom (UDC) for remote appearances

**Scraping approach:** Scrape index page for department links, follow each link to get the current ruling document, download and parse.

**Reusability:** MODERATE. Similar structure to Pattern 2 (PDF links) but with an extra navigation layer. Could share parsing logic with the PDF-link template.

---

### Pattern 6: Case Management Portal with Account Required (Alameda, Sacramento)

**Counties:** Alameda, Sacramento

**How it works:** Tentative rulings are only accessible through the court's case management portal, which requires creating a free account and logging in. Rulings are attached to individual case records rather than published as browsable lists.

**Alameda specifics:**
- Portal: `eportal.alameda.courts.ca.gov` (DomainWeb)
- Free documents: Civil Law and Motion Tentative Rulings, CMC Rulings, Probate Examiner Notes
- Requires registered account
- JavaScript and non-IE browser required (CAPTCHA)
- Some documents free, others paid

**Sacramento specifics:**
- Portal: `prod-portal-sacramento-ca.journaltech.com` (JournalTech)
- All civil tentative rulings available since April 2023
- Free account required
- Includes Presiding Judge Law & Motion rulings and Probate Notes
- Tentative rulings available after 2:00 PM day before hearing

**Scraping approach:** Requires maintaining authenticated sessions. Create account, log in programmatically, navigate to case/ruling search, enumerate by date. More complex than simple HTTP scraping — may need Playwright for JavaScript-rendered portals.

**Reusability:** LOW-MODERATE. DomainWeb and JournalTech are different platforms, each needing a custom scraper. But if multiple counties share the same portal vendor, one template can serve them.

---

## County-by-County Summary Table

| County | Pop. Rank | Pattern | Platform/CMS | Tentative Ruling URL | Difficulty | Priority |
|--------|-----------|---------|-------------|---------------------|------------|----------|
| **Los Angeles** | 1 | ASP.NET Form | Custom | lacourt.ca.gov/tentativeRulingNet/ | Medium | P1 — Highest volume |
| **San Diego** | 2 | Odyssey Portal | Tyler Odyssey | odyroa.sdcourt.ca.gov | Hard | P2 — Need calendar integration |
| **Orange** | 3 | PDF Links | Pantheon/Drupal | occourts.org/.../civil-tentative-rulings | Easy | P1 — Simple, high value |
| **Riverside** | 4 | PDF Links | Pantheon/Drupal | riverside.courts.ca.gov/.../tentative-rulings | Easy | P1 — Same pattern as OC |
| **San Bernardino** | 5 | PDF/HTML Links | Pantheon/Drupal | sanbernardino.courts.ca.gov/.../civil-tentative-rulings | Easy | P1 — Same pattern |
| **Santa Clara** | 6 | Dept Pages + Docs | Pantheon/Drupal | santaclara.courts.ca.gov/.../tentative-rulings | Easy-Med | P2 |
| **Alameda** | 7 | Account Portal | DomainWeb | eportal.alameda.courts.ca.gov | Hard | P3 — Auth required |
| **Sacramento** | 8 | Account Portal | JournalTech | prod-portal-sacramento-ca.journaltech.com | Hard | P3 — Auth required |
| **San Francisco** | 9 | Custom DLL | Custom | webapps.sftc.org/tr/tr.dll | Easy | P2 — Few endpoints |
| **Contra Costa** | 10 | TBD | Custom | cc-courts.org | Medium | P2 |
| **Fresno** | 11 | TBD | Custom | fresnosuperiorcourt.org/tentative_rulings/ | Medium | P2 |
| **Ventura** | 12 | TBD | Custom | ventura.courts.ca.gov | Medium | P2 |
| **San Mateo** | 13 | TBD | Likely Odyssey | sanmateocourt.org/.../lawmotion/ | Medium | P3 |
| **Kern** | 14 | Integrated Search | Custom | kern.courts.ca.gov (Civil tab) | Medium | P3 |
| **San Joaquin** | 15 | Case Inquiry | Custom | stocktoncourt.org | Medium | P3 |

---

## Tyler Odyssey: The Dominant CMS (But Not the Dominant Tentative Ruling Publisher)

Tyler Odyssey has been selected by **28+ of California's 58 superior courts**, covering approximately **70% of California's population**. Key Odyssey counties include:

- Los Angeles (selected 2014, largest court in US)
- San Diego (live, public access portal active)
- San Bernardino (selected 2014, expanded to all case types)
- San Mateo (selected 2014)
- Sonoma (selected)
- Mendocino (selected 2019)
- Shasta (selected 2019)
- Yolo (selected 2019)
- And 20+ others

**Critical insight:** Odyssey adoption does NOT mean tentative rulings are published through the Odyssey portal. Many Odyssey courts continue publishing tentatives via their court website (PDFs, HTML pages) separately from the Odyssey case management system. The tentative ruling publication mechanism is often a court-level editorial/administrative decision independent of the backend CMS.

**However**, as more courts fully deploy Odyssey (including the public access module), we may see a convergence where tentative rulings become accessible through a standardized Odyssey portal interface. This would be ideal for Judgemind — one Odyssey scraper template could cover many courts.

**California's Master Services Agreement (MSA)** makes Tyler the preferred case management vendor for all 58 superior courts, so Odyssey adoption will likely continue expanding.

---

## Existing Competitors Scraping CA Tentatives

| Service | Coverage | Price | Started | Notes |
|---------|----------|-------|---------|-------|
| **Trellis.law** | 46 states, 3,000+ courts | $70–$200/mo | 2016 | 10-year archive. The benchmark. |
| **tentativerulings.org** | 25 CA counties, ~200 judges | Subscription (paid) | April 2019 | ~25K rulings/year. Business day capture. |
| **Bench Reporter** | Primarily LA County | $39/mo | Unknown | Crowdsourced tagging by motion type. |
| **rulings.law** | CA (LA, SD, OC confirmed) | Free (ad-supported) | ~2022 | Backdrop CMS. ~180 judge folders. Uneven activity. Open directory listing at /rulings/. Pending your outreach for potential partnership. |
| **coordinatedlegal.com** | Directory only (links) | Free | 1999 | Links to all 58 county court sites. Not a scraper — just a portal. Useful as our master county URL reference. |

---

## Recommended Scraper Architecture

Based on this investigation, the scraper framework should support **parameterized templates** rather than one-off implementations per court:

### Template 1: PDF-Link Scraper
**Covers:** Orange, Riverside, San Bernardino, Santa Clara, and likely 10+ smaller counties
**How:** Scrape an index page for PDF links → download PDFs → extract text → parse rulings
**Parameters:** Index URL, link selector, PDF URL pattern, judge name regex, courthouse mapping

### Template 2: ASP.NET Form Scraper
**Covers:** LA County (and potentially other legacy ASP.NET courts)
**How:** GET page for tokens → enumerate dropdown → POST per option → parse HTML response
**Parameters:** Form URL, dropdown field names, ViewState handling, response parser

### Template 3: Static Endpoint Scraper
**Covers:** San Francisco, and courts with direct-URL tentative ruling pages
**How:** GET each known endpoint URL → parse HTML response
**Parameters:** List of endpoint URLs, response parser, CAPTCHA handling if needed

### Template 4: Odyssey Portal Scraper
**Covers:** San Diego, and potentially other Odyssey-portal courts
**How:** Navigate Odyssey public access portal → search by hearing date → extract tentatives per case
**Parameters:** Portal URL, search parameters, authentication if required

### Template 5: Authenticated Portal Scraper
**Covers:** Alameda, Sacramento, and other account-required portals
**How:** Maintain authenticated session → navigate case/ruling search → enumerate by date
**Parameters:** Portal URL, credentials, login flow, search navigation, Playwright if needed

### Shared Components (All Templates)
- Content hashing (SHA-256) for version tracking
- Raw content archival to object storage
- Structured field extraction (case number, judge, department, hearing date, ruling text)
- Health reporting (success/failure, record count, response time)
- Configurable polling schedule
- Error handling with exponential backoff

---

## Recommended Implementation Order

### Phase 1 (Weeks 1–3): Highest ROI, Easiest Implementation
1. **LA County** (Pattern 1) — Highest volume, architecture already understood
2. **Orange County** (Pattern 2) — Easy PDF scraping, 3rd largest county
3. **Riverside** (Pattern 2) — Same template as OC with different parameters
4. **San Bernardino** (Pattern 2) — Same template, 5th largest county

These four counties cover **~60% of California's population** and three of them share the same scraper template.

### Phase 2 (Weeks 3–6): Medium Difficulty, High Value
5. **San Francisco** (Pattern 3) — Few endpoints, simple GETs
6. **Santa Clara** (Pattern 5) — Similar to Pattern 2 with extra navigation
7. **San Diego** (Pattern 4) — Harder but 2nd largest county
8. **Contra Costa, Fresno, Ventura** — Investigate and classify

### Phase 3 (Weeks 6–10): Harder or Lower Priority
9. **Alameda** (Pattern 6) — Auth required
10. **Sacramento** (Pattern 6) — Auth required
11. **Remaining counties** from the coordinatedlegal.com directory — classify and implement

---

## Open Questions

1. **San Diego Odyssey portal:** Can we enumerate hearings by date to get a list of cases with tentatives, or do we need case numbers first? Needs hands-on investigation of the portal interface.

2. **Alameda DomainWeb terms of service:** Does programmatic access violate their TOS? Need to review.

3. **Sacramento JournalTech portal:** Same question — is programmatic access permitted?

4. **rulings.law partnership:** Pending your outreach. Their existing LA/SD/OC archive could accelerate our historical depth significantly.

5. **Tyler Odyssey API access:** Tyler offers APIs for some jurisdictions. Worth investigating whether any CA courts expose tentative rulings via an official API rather than requiring web scraping.

6. **Smaller county coverage:** The coordinatedlegal.com directory lists tentative ruling links for ~25+ counties. Many of these may be simple PDF-link or HTML patterns that fit Template 1 or Template 3 with minimal customization.

---

## Data Sources Used

- Direct investigation of court websites for LA, Orange, San Diego, San Francisco, Sacramento, Santa Clara, Alameda, Riverside, San Bernardino
- coordinatedlegal.com — master directory of all 58 CA county court links
- tentativerulings.org — competitor coverage information
- Tyler Technologies press releases and SEC filings — Odyssey adoption data
- Previous LA County investigation (companion document)
