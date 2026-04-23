# LA County Superior Court — Scraping Investigation
## Judgemind Initial Data Reconnaissance
**Date:** March 1, 2026

---

## 1. Site Architecture Overview

LA Superior Court operates across **two domains**, each serving different parts of the system:

- **lacourt.ca.gov** — Newer site, appears to be a client-side rendered SPA (JavaScript-heavy). Hosts the tentative rulings system and landing pages. Many pages return minimal HTML to a fetcher because content is rendered in the browser.
- **lacourt.org** — Older ASP.NET pages. Hosts case summaries, criminal case access, online services FAQ, privacy policy, and some navigation pages. Server-rendered, more scraper-friendly.

**Key implication:** The SPA architecture on lacourt.ca.gov means a headless browser (Playwright/Selenium) will likely be required for many endpoints. Simple HTTP requests won't get rendered content.

---

## 2. Tentative Rulings — The Prize

**URL:** `https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil`

This is the single most important endpoint. It's an ASP.NET WebForms page (note the `.aspx` extension) with two search methods:

### Search Method 1: By Case Number
- Text input field, enter a case number, click search
- Returns tentative ruling(s) for that case if any are currently published

### Search Method 2: By Location/Department/Hearing Date dropdown
- A single dropdown containing all currently published tentative rulings
- Format: `(Courthouse Name: Dept. XX) Month Day, Year`
- Select an entry, click search, get the ruling text

### What the Dropdown Reveals (captured January 27, 2026)

The dropdown is a **goldmine** — it's essentially a complete index of every currently published tentative ruling. From the captured data:

**Active Courthouses Publishing Tentatives:**

| Courthouse | Departments Observed |
|---|---|
| Alhambra | 3, T, X |
| Beverly Hills | 205, 207 |
| Chatsworth | F43, F46, F49 |
| Compton | E |
| Gov. Deukmejian (Long Beach) | S25, S27 |
| Inglewood | 5 |
| Michael Antonovich (Antelope Valley) | A14 |
| Norwalk | C, L, R, SEP, Y |
| Pasadena | P |
| Pomona South | G, H, O |
| Santa Monica | I, M, N, O, P |
| Spring Street | 9, 14, 25, 26, 27, 28 |
| Stanley Mosk | 3, 15, 17, 19, 20, 26, 30, 31, 32, 34, 37, 39, 40, 45, 47, 48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 61, 68, 71, 72, 73, 74, 76, 78, 85, 86 |
| Torrance | B, E, M, P |
| Van Nuys East | B, I, T |
| Van Nuys West | 107 |
| West Covina | 6 |

**Key observations:**
- Stanley Mosk dominates (~35 departments posting tentatives on a single day)
- Most departments post only 1 day ahead
- Some departments post multiple days ahead (Pomona South Dept. H posts weeks out — dates observed through March 12, 2026)
- Not every department publishes tentatives online — some only make them available in the courtroom

### Scraping Strategy for Tentative Rulings

**Option A: Enumerate the dropdown.** The dropdown itself contains every available (courthouse, department, date) tuple. A scraper can:
1. Fetch the main page
2. Parse all dropdown options
3. For each option, submit the form and capture the ruling text
4. This gives complete coverage of everything currently published

**Option B: Poll by case number.** If we know case numbers with upcoming hearings (from calendar data), we can query by case number directly. This is useful as a secondary check.

**Recommended approach:** Option A as the primary method (daily full sweep of the dropdown), supplemented by Option B for cases we're actively tracking.

**ASP.NET WebForms complication:** The page uses ASP.NET postback mechanism (`__VIEWSTATE`, `__EVENTVALIDATION`). The scraper will need to:
1. GET the page to obtain the ViewState tokens
2. POST with the selected dropdown value and the ViewState tokens
3. Parse the response HTML for the ruling content

This is standard ASP.NET scraping — well-understood pattern.

---

## 3. Case Summary / Docket Data

**URL:** `https://www.lacourt.org/casesummary/ui/index.aspx?casetype=civil`

The civil case summary system provides docket information when you enter a case number. This is **free** — no registration or payment required for case summary lookups.

**What it returns (per search results and FAQ):**
- Case parties
- Case type
- Filing date
- Filing location
- Case status
- Docket entries / case events

**Limitation:** You need a case number to search. There is no free browse-all or bulk download.

---

## 4. Paid Services (Scraping Barriers)

Several services require registration and per-search fees:

| Service | Fee Structure | What It Returns |
|---|---|---|
| **Search for Case Number by Name** | Per-search fee (sliding scale for registered users) | Litigant name, case type, filing date, location, doc count |
| **Case Document Images** | Per-document fee | Scanned copies of filed documents (PDFs) |
| **Criminal Case Summary** | Free but reCAPTCHA protected | Criminal case information |

**Key insight:** Name search requires payment, but **case summary by case number is free**. This means if we can obtain case numbers from other sources (tentative rulings, calendars, public filings), we can look up docket info without fees.

---

## 5. Anti-Scraping Measures

**Confirmed measures:**
- **reCAPTCHA on criminal case access** during business hours (7:00 AM to 7:00 PM daily). Disabled after hours.
- **Session cookies** required for case summaries, calendars, and other interactive pages
- **ASP.NET ViewState** on WebForms pages (standard anti-CSRF, not specifically anti-scraping, but complicates simple HTTP scraping)

**From the architecture spec (historical knowledge):**
- Orange County historically deployed CAPTCHAs 9 AM–5 PM Pacific only
- Some courts rate-limit during business hours

**Current status for civil tentative rulings:** No CAPTCHA observed on the tentative rulings page. It appears to be freely accessible.

---

## 6. Case Number Format

LA County uses a structured case numbering system (captured from the court's official PDF matrix):

### New Format (current): `YYLLCC#####`
- **YY** = 2-digit year
- **LL** = Location code (e.g., ST = Stanley Mosk, SM = Santa Monica, CH = Chatsworth)
- **CC** = Case type (e.g., CV = Civil Unlimited, LC = Civil Limited, FL = Family Law)
- **#####** = 5-digit sequence number

**Example:** `25STCV13276` = 2025, Stanley Mosk, Civil Unlimited, case 13276

### Old Format (General Jurisdiction): District letter + Case type letter + numbers
- First letter = District (B=Central, E=NE, G=E, K=NW, etc.)
- Second letter = Case type (C=Civil, D=Family, P=Probate, etc.)

### Location Codes (18 courthouses):
Alhambra (AH), Antelope Valley (AV), Beverly Hills (BH), Burbank (BB), Chatsworth (CH), Compton (CM), Inglewood (IW), Long Beach (LB), Norwalk (NW), Pasadena (PD), Pomona South (PS), Santa Monica (SM), Spring Street (SS), Stanley Mosk (ST), Torrance (TR), Van Nuys East (VE), Van Nuys West (VW), West Covina (WC)

**This is extremely useful for scraping** — we can generate plausible case number ranges for bulk lookups.

---

## 7. Other Free Data Sources on lacourt.org

| Endpoint | Data Available | Scraper Difficulty |
|---|---|---|
| **Tentative Rulings (Civil)** | Full ruling text, case number, department, hearing date | Medium (ASP.NET postback) |
| **Civil Case Summary** | Docket entries, parties, dates, status (by case number) | Medium (session-based) |
| **Case Calendar** | Hearing schedules by courthouse/department | Low-Medium |
| **Courtroom Information** | Judge assignments, department rules, contact info | Low (static pages) |
| **Filing Court Locator** | Which courthouse handles which case types | Low (static) |
| **Judicial Officers** | Judge listings | Low (static) |

---

## 8. Competitive Landscape for CA Tentative Rulings

We're not the only ones who've figured out this data is valuable:

| Competitor | Model | Coverage |
|---|---|---|
| **Trellis.law** | $70–$200/mo | 10 years of LA County tentatives + 46 states |
| **tentativerulings.org** | Subscription | 25+ CA counties, ~25,000 new rulings/year, started 2019 |
| **Bench Reporter** | $39/mo | Primarily LA County, crowdsourced tagging |
| **rulings.law** | Ad-supported (free) | CA tentative rulings database |

**rulings.law is interesting** — it's described as a "free, searchable, online database of California tentative rulings" that relies on advertising revenue. Worth investigating their coverage and approach.

---

## 9. Immediate Next Steps

### Priority 1: Build LA County Tentative Ruling Scraper
1. **Fetch the tentative rulings dropdown page** daily (or multiple times daily)
2. **Parse all dropdown options** to enumerate every published ruling
3. **Submit form for each option** and capture the full ruling HTML
4. **Extract structured fields:** case number, courthouse, department, hearing date, judge name, ruling text, motion type, parties
5. **Store raw HTML** and extracted text
6. **Content hash** each ruling for version tracking

### Priority 2: Build LA County Case Summary Lookup
1. For every case number found in tentative rulings, **look up the case summary**
2. Extract docket entries, parties, case type, filing date, assigned judge
3. This gives us the relational data that connects rulings to cases

### Priority 3: Map Out California Counties
1. Identify which other CA counties publish tentative rulings online
2. Catalog their URL patterns and technical approaches
3. Prioritize by litigation volume: Orange County, San Diego, San Francisco, Sacramento, Santa Clara likely next

### Priority 4: Understand the Actual Ruling Content
- We need to actually see what a rendered tentative ruling looks like on this site
- The ruling text will determine what NLP we need: entity extraction patterns, motion type classification, outcome parsing
- This requires using Playwright to render the ASP.NET page and capture the output

---

## 10. Questions for You (Based on Your Experience)

1. **Has the tentative ruling page structure changed significantly since 2016?** The ASP.NET WebForms architecture looks like it could be the same system.

2. **In 2016, did you scrape by enumerating the dropdown, by case number, or both?** The dropdown enumeration approach seems most reliable for complete coverage.

3. **How did you handle the timing?** Tentatives typically post the day before the hearing — did you scrape in the evening, early morning, or multiple times?

4. **Were there courts/departments that posted tentatives in non-standard ways** (PDF uploads, different page structures) that wouldn't show in the dropdown?

5. **The case summary page — was it always free by case number, or has that changed?** The name search has always been paid, but I want to confirm the case number lookup path.

6. **rulings.law** — is this connected to any of the other players, or is it independent?
