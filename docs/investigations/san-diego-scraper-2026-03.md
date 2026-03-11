# Investigation: San Diego County Odyssey Portal Scraping Approach

**Issue:** #154
**Date:** March 2026
**Status:** Complete

---

## Executive Summary

San Diego County (CA's 2nd largest) publishes tentative rulings through its Tyler Odyssey "Register of Actions" portal at `odyroa.sdcourt.ca.gov`. The portal is entirely behind **Cloudflare Bot Management** with interactive JavaScript challenges, making direct HTTP scraping impossible. However, the court also operates a separate, **fully scrapable court calendar system** at `sandiego.courts.ca.gov/portal/online/calendar/` that lists every case hearing by date, department, case number, event type, judge, parties, and attorneys. This calendar provides the enumeration mechanism the original investigation identified as the key challenge.

**Recommended approach:** A two-phase scraper that (1) enumerates cases with hearings from the public calendar (plain HTTP, no bot protection), then (2) retrieves tentative rulings from the Odyssey ROA portal using Playwright to solve Cloudflare challenges. This is feasible but represents medium-high difficulty due to the Cloudflare requirement.

---

## Investigation Findings

### 1. Hearing Calendar

**Yes — a date-based hearing calendar exists and is fully scrapable.**

The court operates a legacy calendar system at `http://www.sandiego.courts.ca.gov/portal/online/calendar/`. This is separate from the Odyssey portal and has **no bot protection** — plain HTTP GET requests return full HTML responses.

**Calendar structure:**
- Static HTML pages generated daily (regenerated at ~1:00 AM and ~5:20 AM)
- Separate pages per division: Central (`F_SVCAL{N}`), North County (`F_VVCAL{N}`), East County (`F_EVCAL{N}`), South County (`F_BVCAL{N}`)
- `{N}` = day number (1-5) covering a rolling 5-business-day window
- Each page lists all hearings for that division and date

**Fields available per hearing:**
- **Case number** (e.g., `24CU016153C`)
- **Event type** (e.g., "Motion Hearing", "Demurrer/Motion to Strike", "Summary Judgment")
- **Hearing time**
- **Department number**
- **Judge name** (e.g., "Judge MATTHEW C. BRANER")
- **Case title/entitlement** (e.g., "Smith vs Jones")
- **Party names** with role (PL/DF)
- **Attorney names**

**Calendar search:** A Perl-based search endpoint at `/scripts/seekcalendar.pl` accepts query parameters for text search (`g=`), date filtering (`d=1` today, `d=2` tomorrow), division (`c=v` civil), judge name (`j=`), party name (`p=`), and attorney name (`a=`). Returns fixed-width text results.

**URL patterns:**
- Central civil: `http://www.sandiego.courts.ca.gov/portal/online/calendar/f_svcal{1-5}.html`
- North County civil: `.../F_VVCAL{1-5}.html`
- East County civil: `.../F_EVCAL{1-5}.html`
- South County civil: `.../F_BVCAL{1-5}.html`
- Calendar search: `http://www.sandiego.courts.ca.gov/scripts/seekcalendar.pl?z=portal&g={searchterm}`

### 2. Tentative Ruling Access

**Rulings are accessed through the Odyssey ROA portal, which requires solving Cloudflare challenges.**

The court's tentative rulings page at `https://www.sdcourt.ca.gov/sdcourt/civil2/civiltentativerulings` states:

> "Tentative rulings are available through the online Register of Actions by 4:00 p.m. before the scheduled motion day."
>
> "Tentative rulings are now available through the online Register of Actions. There is no cost to view tentative rulings."

Both "View Tentative Rulings" and "View Rulings" links point to `https://odyroa.sdcourt.ca.gov/`.

**URL patterns (inferred from Tyler Odyssey standard patterns):**
- Portal home: `https://odyroa.sdcourt.ca.gov/portal/`
- Smart search: `https://odyroa.sdcourt.ca.gov/portal/Home/SmartSearch?searchString={caseNumber}`
- Case detail: `https://odyroa.sdcourt.ca.gov/portal/Home/CaseDetail/{caseId}`
- Hearing search: `https://odyroa.sdcourt.ca.gov/portal/Home/HearingSearch`

Once past Cloudflare, the standard Odyssey portal UI would allow searching by case number to reach the Register of Actions, where tentative rulings appear as entries. The exact format of the tentative ruling within the ROA (inline text, PDF attachment, or linked document) could not be determined due to the Cloudflare block.

### 3. JavaScript Requirements

**Full browser (Playwright) is REQUIRED for the Odyssey portal.**

The Odyssey portal (`odyroa.sdcourt.ca.gov`) is protected by **Cloudflare Bot Management** with:
- **Interactive JavaScript challenge** (`cf-mitigated: challenge` header, `window._cf_chl_opt` challenge script)
- **Bot management cookies** (`__cf_bm` with `HttpOnly; Secure` flags, scoped to `.sdcourt.ca.gov`)
- **Challenge platform scripts** loaded from `/cdn-cgi/challenge-platform/`

The root URL shows a static "maintenance" page (may be a Cloudflare custom block page), while sub-paths like `/portal/Home/SmartSearch` serve the interactive Cloudflare challenge page that requires JavaScript execution.

**The court calendar system does NOT require JavaScript** — it returns plain server-rendered HTML via standard HTTP GET requests. No cookies, no AJAX, no form tokens.

### 4. Rate Limiting / Bot Detection

**Cloudflare Bot Management on Odyssey portal; no protection on calendar system.**

- **Odyssey portal (`odyroa.sdcourt.ca.gov`):** Cloudflare Bot Management blocks all programmatic access. The `cf-mitigated: challenge` response header confirms active bot mitigation. Even with a browser User-Agent header, the response is a 403 with a JavaScript challenge. Solving this requires a real browser engine (Playwright) that can execute the Cloudflare challenge JavaScript, pass the Turnstile check, and maintain the resulting `cf_clearance` cookie.
- **Court calendar (`sandiego.courts.ca.gov/portal/online/calendar/`):** No bot protection observed. Standard HTTP requests with any User-Agent succeed. The `<meta name="ROBOTS" content="NOINDEX,NOFOLLOW">` tag discourages search engine indexing but does not block programmatic access.
- **MARS scheduling system (`mars.sdcourt.ca.gov`):** Also behind Cloudflare challenge (same pattern as Odyssey).

### 5. Odyssey API

**No official Tyler API endpoint was discovered for San Diego.**

Tyler Technologies offers API access for some Odyssey jurisdictions, but San Diego's portal does not expose any discoverable API endpoints. The portal is a standard Odyssey ASP.NET MVC web application, not an API gateway. No `/api/` or `/odata/` endpoints were found.

Tyler's eFileCA system (used for e-filing in California) is a separate system from the ROA portal and does not provide public access to tentative rulings.

### 6. Volume Estimate

**Approximately 100-150 cases with tentative rulings per week, concentrated on Fridays.**

Based on the civil calendar for a sample week (March 9-13, 2026):

| Day | Total Civil Cases | Motion-Type Hearings | Likely Tentative Rulings |
|-----|-------------------|---------------------|--------------------------|
| Monday (3/10) | 77 | 16 ex parte | ~0 (ex parte = day-of) |
| Tuesday (3/10) | 77 | 16 ex parte, 1 discovery | ~0 |
| Wednesday (3/11) | 100 | 20 ex parte | ~0 |
| Thursday (3/12) | 98 | 17 ex parte, 3 motions | ~3 |
| **Friday (3/13)** | **606** | **44 motions, 33 demurrers, 14 discovery, 3 SJ, 1 class cert** | **~95** |

**Friday is the dominant motion day** with 606 total cases and approximately 95 that would have tentative rulings (motions, demurrers, discovery hearings, summary judgment). Other weekdays have primarily administrative hearings (CMCs, name changes, restraining orders, ex partes) that typically do not generate written tentative rulings.

**Departments observed (Central Division only):**
- IC departments: C-60 through C-68, C-71, C-72, C-74, C-75 (12 departments)
- Non-IC departments: 201, 2102, 2103
- North County: N-02, N-27, N-28, N-29, N-31
- East County: E-21
- South County: S-02

**Judges observed:** 17 unique judges across all divisions.

---

## Recommended Scraping Approach

### Two-Phase Architecture

#### Phase 1: Calendar Enumeration (Plain HTTP)

1. **Daily at 4:30 PM:** Fetch tomorrow's civil calendar pages for all 4 divisions (Central, North, East, South) — 4 HTTP GET requests.
2. Parse the HTML tables to extract: case number, event type, department, judge, time, case title, parties, attorneys.
3. Filter for motion-type events likely to have tentative rulings: "Motion Hearing", "Demurrer/Motion to Strike", "Summary Judgment/Summary Adjudication", "Discovery Hearing", "Motion to Quash", "Motion for Sanctions", "Motion Hearing to Certify/Decertify Class Action".
4. Store the filtered case list with all metadata.

This phase is simple, reliable, and provides the case enumeration that was identified as the key challenge.

#### Phase 2: Tentative Ruling Retrieval (Playwright)

1. **Daily at 4:15 PM** (after the court's 4:00 PM posting deadline): For each case from Phase 1, use Playwright to:
   a. Navigate to `https://odyroa.sdcourt.ca.gov/portal/Home/SmartSearch?searchString={caseNumber}`
   b. Solve the Cloudflare challenge (Playwright with `stealth` mode)
   c. Navigate to the case's Register of Actions
   d. Find and extract the tentative ruling entry
   e. Download any attached document (PDF or text)
2. Maintain a single browser session with the `cf_clearance` cookie across all case lookups within a run to avoid repeated Cloudflare challenges.
3. Archive raw content to S3 before any processing.

### Alternative Approaches Considered

**A. Playwright-only (skip calendar):** Navigate directly to the Odyssey hearing search to enumerate cases. Rejected because: the calendar system provides the same data without Cloudflare overhead, and the calendar also provides party/attorney information that the Odyssey hearing search may not surface as easily.

**B. RSS/Email monitoring:** Some Odyssey portals offer RSS feeds or email notifications for case events. No evidence of this feature in San Diego's configuration.

**C. Third-party data providers:** The Tyler/Catalis Data Exchange Program provides bulk data access to some jurisdictions. This requires a formal data sharing agreement and is typically restricted to government agencies and approved legal technology providers. Worth investigating as a long-term strategy but not viable for immediate scraping.

### Cloudflare Bypass Considerations

Playwright with stealth plugins (e.g., `playwright-extra` with `stealth` plugin, or `undetected-playwright`) can solve Cloudflare's interactive JavaScript challenges. Key considerations:

- **Session reuse:** A single Cloudflare challenge solve produces a `cf_clearance` cookie valid for ~30 minutes. A single browser session can serve all case lookups in one run.
- **Residential IP vs datacenter IP:** Cloudflare's bot scoring is more aggressive toward datacenter IPs (like AWS). May need a residential proxy or to run the Playwright portion from a non-datacenter IP.
- **Challenge type escalation:** Cloudflare may escalate from JS challenge to CAPTCHA (Turnstile) for persistent automated access. Monitor for this.
- **Respect rate limits:** Space requests 2-5 seconds apart to avoid triggering rate-based blocks.

### Implementation Steps

1. **Calendar scraper** (new, Pattern 7: Calendar + Portal hybrid):
   - Fetch 4 division calendar pages via httpx
   - Parse HTML tables for case metadata
   - Filter for motion-type events
   - Store case list with all fields

2. **Odyssey ROA scraper** (requires Playwright):
   - Accept case numbers from the calendar scraper
   - Navigate Odyssey portal with Playwright
   - Solve Cloudflare challenge once per session
   - Extract tentative ruling for each case
   - Archive raw content

3. **Combined pipeline**: Calendar enumeration runs first, feeds case list to ROA scraper.

### Reusable Odyssey Template

If the Cloudflare challenge approach works for San Diego, the same pattern could apply to **other Odyssey-portal courts** that may deploy similar protections in the future. The template would be parameterized by:
- Portal URL (e.g., `odyroa.sdcourt.ca.gov`)
- Search URL pattern
- Case detail URL pattern
- ROA navigation selectors
- Tentative ruling identification pattern within the ROA

However, not all Odyssey courts use Cloudflare, and many publish tentatives separately from their Odyssey portal (see `docs/specs/ca-county-investigation.md` for details). The template's reusability is moderate — useful for Odyssey-portal courts, but each court's configuration varies.

---

## Difficulty Estimate

**Medium-High**

- Calendar enumeration: **Easy** — plain HTTP, static HTML, well-structured tables, no authentication.
- Odyssey ROA retrieval: **Hard** — Cloudflare Bot Management requires Playwright with stealth, session management, potential proxy needs, and ongoing maintenance as Cloudflare updates its challenges.
- Field extraction from ROA: **Unknown** — the tentative ruling format within the ROA could not be examined due to the Cloudflare block. May be inline text, a PDF attachment, or a linked document. This needs to be determined during implementation after Cloudflare access is established.

**Estimated implementation time:** 2-3 sessions (1 for calendar scraper + Cloudflare proof of concept, 1-2 for full ROA integration and field extraction).

---

## Blockers and Risks

1. **Cloudflare may escalate protections.** If the court or Cloudflare detects persistent automated access, they may switch from JS challenge to CAPTCHA (Turnstile), which is significantly harder to solve programmatically. Mitigation: low request volume (~100 cases/week, concentrated on Friday afternoons), residential proxy if needed.

2. **Tentative ruling format is unknown.** Without access to the ROA, we cannot confirm how tentative rulings appear. They might be free-text entries, PDF attachments, or links to documents. The scraper's field extraction logic depends on this. Mitigation: build the calendar scraper first, then investigate the ROA format with Playwright.

3. **Datacenter IP blocking.** AWS/cloud IPs are commonly blocked by Cloudflare's bot scoring. The Playwright scraper may need to run from a residential IP or use a proxy service. Mitigation: test from a residential IP first to confirm the approach works, then determine if a proxy is needed for production.

4. **Maintenance page on portal root.** The `odyroa.sdcourt.ca.gov` root URL shows a "This website is currently unavailable due to maintenance" page. This could indicate scheduled maintenance (temporary), a permanent block page for non-browser clients, or an intentional Cloudflare custom block. The portal sub-paths serve the Cloudflare challenge instead, suggesting the portal itself is operational but protected.

---

## Decisions Needing Human Input

1. **Cloudflare bypass policy:** Is using Playwright + stealth to solve Cloudflare challenges acceptable for this project? The court publishes tentative rulings as free public records, but Cloudflare's protection may reflect the court's (or Tyler's) intent to limit automated access. The ToS for the Odyssey portal should be reviewed.

2. **Proxy budget:** If datacenter IPs are blocked and a residential proxy is needed, this adds ongoing cost. Approximate: $5-20/month for the low volume (~100 requests/week).

3. **Priority relative to other counties:** Given the medium-high difficulty and Cloudflare complexity, should San Diego be prioritized over easier counties like Fresno, Contra Costa, and Ventura (all P2, all easier)?

---

## References

- `docs/specs/ca-county-investigation.md` — Pattern 3 section (Tyler Odyssey)
- `docs/investigations/remaining-ca-counties-2026-03.md` — related county investigations
- Court tentative rulings page: `https://www.sdcourt.ca.gov/sdcourt/civil2/civiltentativerulings`
- Court calendar system: `http://www.sandiego.courts.ca.gov/portal/online/calendar/`
- Odyssey ROA portal: `https://odyroa.sdcourt.ca.gov/` (Cloudflare-protected)
