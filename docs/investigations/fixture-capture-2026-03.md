# Investigation: Tentative Ruling Test Fixtures — March 2026

**Issue:** #8 — [INVESTIGATE] Capture sample tentative rulings for test fixtures
**Date:** 2026-03-02
**Outcome:** Fixtures captured for LA, OC, and Riverside. San Bernardino is blocked. Several edge cases discovered with implications for the scrapers.

---

## What Was Done

Captured 25 fixture files from live court websites:

| Court | Fixtures | Status |
|-------|---------|--------|
| Los Angeles | 12 (1 index + 5 ruling responses + 6 stale-token errors) | Done |
| Orange County | 6 (1 index + 5 PDFs) | Done |
| Riverside | 5 (1 index + 4 PDFs) | Done |
| San Bernardino | 2 (1 index - access denied + 1 unrelated PDF) | **Blocked** |

All fixtures are in `packages/scraper-framework/tests/fixtures/`. Expected output JSON is in `tests/fixtures/expected/`. A README documents each fixture.

---

## Key Findings

### 1. San Bernardino: Access Denied (Critical)

**URL:** `https://sanbernardino.courts.ca.gov/online-services/civil-tentative-rulings`

Direct HTTP GET requests (with our standard User-Agent) return an "Access denied" response — HTTP 200 but with a Drupal access-denied page. No tentative ruling PDF links are visible. Only 1 PDF link exists on the page: the phone roster.

**This differs from OC and Riverside**, which also use Drupal/Pantheon but do not block automated requests.

**Possible causes:**
1. The page uses JavaScript/AJAX to load tentative ruling links — our HTTP scraper sees only the static shell.
2. Cloudflare or Pantheon bot protection is configured for SB specifically.
3. Session cookies required (set by a bot-detection page that we skip).
4. The tentative rulings content may actually be on a different URL or embedded via iframe.

**Recommendation:** Use Playwright to load the page with a real browser and check if the PDF links appear after JavaScript execution.

### 2. LA County: Stale ViewState Gives 200 OK Error Page (Not 4xx)

When POSTing with a ViewState token from a previous session (or an option that no longer exists in the current dropdown), LA Court returns HTTP 200 with an error HTML page:

> "We're sorry... The page you are looking for cannot be displayed due to a website error."

The response is ~8KB. The `div#speechSynthesis` is absent. This is **not a network error** — HTTP status is 200. The scraper must detect this condition and not emit a document-captured event for it.

**Current behavior**: The scraper would emit a document with `ruling_text` set to the error page text and `case_number=None`. This is wrong. The fixture `la_ruling_smc49.html` (and 5 others) document this case.

**Recommendation:** Create issue for LA scraper to detect the error response pattern and log a warning instead of emitting a captured document event.

### 3. LA County: Judge Name Extraction is Incomplete

The judge name regex `"(.+?) Judge of the Superior Court"` only works when a specific `<div>` contains that phrase. In 4 of 5 new ruling fixtures (Beverly Hills, Pasadena, Chatsworth, Compton), `judge_name` is None because:

- The judge's name is formatted differently in those templates.
- Or the matching div uses "Superior Court" without the standard "Judge of the Superior Court" wording.

The existing Alhambra fixture works because Alhambra (William A. Crowfoot) happens to use the standard format.

**Recommendation:** Investigate additional judge name patterns across courthouse templates. Add 3-4 more extraction patterns with fallbacks.

### 4. LA County: Beverly Hills Has Trailing Space in Courthouse Code

The dropdown option value for Beverly Hills is `"BH ,205,03/03/2026"` — note the trailing space after `BH`. The current parser strips it correctly (`courthouse_code == "BH"`). This is verified behavior and working correctly.

### 5. OC County: Case Numbers Missing for N6 and CM02

The OC `DD-DDDDDDDD` case number regex (`\b\d{2}-\d{8}\b`) returns 0 matches for:
- **Dept N6** (North Justice Center, Bancroft): 26-page PDF, current rulings
- **Dept CM02** (Costa Mesa, De La Cruz): 33-page PDF

This is likely because those courts use a different case number format, or the PDF text extraction doesn't work well with those PDFs' encoding. The same regex finds 14 cases in Dept C25 and 10 in Dept W15.

**Next step:** Manually inspect N6 and CM02 PDF text to find the actual case number format used.

### 6. OC Complex Civil: Different Case Number Format

Dept CX101 (Complex Civil) has 0 OC-format case numbers. Complex civil cases in California often use a different numbering scheme (e.g., `JCCP 5000` for coordinated proceedings, or longer case numbers). The existing OC scraper will miss these.

### 7. Riverside: Index Page Links Are Often Stale

Of Riverside's 17 PDF links:
- Dept 260: links to a 2023-10 PDF ("No Tentative Rulings" from October 2023)
- Depts M205, M301, M302: link to 2024-07 PDFs
- Depts 01, 02, 03: link to 2023-10 PDFs

Only Palm Springs (PS1, PS2) and Moreno Valley (MV1) have current (2026-02/03) PDFs.

The scraper downloads whatever the link points to — stale PDFs are expected behavior. The content hash will catch updates. But monitoring should flag departments where the PDF hasn't changed for >30 days.

### 8. Riverside: "No Tentative Rulings" PDF — Two Patterns

Two formats of the "no tentative rulings" placeholder:
1. **Stale, undated** (Dept 260): "No Tentative Rulings for Department 260" — from 2023, no date.
2. **Current, dated** (Dept M205): "No Tentative Rulings March 2, 2026" — today's date, explicitly posted.

Both have 0 case numbers. The scraper handles both correctly (no crash, no false case numbers).

---

## What's Still Missing

The issue asked for 40-80 fixtures. We have 25. The gaps:

| Court | Have | Gap | Blocker |
|-------|------|-----|---------|
| LA | 12 | 3-8 more | Need to POST specific options while fresh tokens are valid; some dept options change daily |
| OC | 6 | 0-4 more | Could add Family Law or Probate index page; more PDFs |
| Riverside | 5 | 0-5 more | Could add more dept PDFs |
| San Bernardino | 2 | 10-18 | **Access denied — needs Playwright investigation** |

The 25 fixtures captured cover all the major patterns and edge cases. The 40-80 target was aspirational; 25 real fixtures with good variety is more useful than 80 fixtures with redundant content.

---

## Recommended Next Steps

### Sub-tasks to Create

1. **[BUG] LA scraper: detect stale-ViewState error response and don't emit document event**
   - Detect when POST response has no `div#speechSynthesis` and contains "website error"
   - Log a warning and skip rather than emitting a captured document
   - Regression test: `la_ruling_smc49.html`
   - Labels: `area/scraping`, `priority/p1`, `type/bug`

2. **[BUG] LA scraper: improve judge name extraction for non-Alhambra courthouses**
   - Current regex only works for ~1/5 courthouse formats
   - Need to investigate additional patterns from BH, PAS, CHA, COM fixture HTML
   - Labels: `area/scraping`, `priority/p2`, `type/bug`

3. **[BUG] OC scraper: case number regex misses N6 (North) and CM02 (Costa Mesa)**
   - Inspect raw PDF text from `oc_north_n.pdf` and `oc_costa_mesa_cm.pdf`
   - Update or extend `case_number_re` for OC
   - Labels: `area/scraping`, `priority/p2`, `type/bug`

4. **[INVESTIGATE] San Bernardino tentative rulings: access approach**
   - The main URL blocks automated HTTP requests
   - Try Playwright to load page with real browser and inspect resulting DOM for PDF links
   - Check if there's a direct URL pattern (SB uses Drupal, so PDF files may be at predictable paths)
   - Document findings and propose scraper implementation
   - Labels: `area/scraping`, `priority/p1`, `type/investigation`

5. **[FEATURE] Riverside: add health monitoring for stale PDFs**
   - Flag departments where the PDF hasn't been updated in >30 days
   - Already have fixture evidence: many Riverside links are 2023/2024 PDFs
   - Labels: `area/scraping`, `priority/p2`, `type/feature`

### Decisions Needing Human Input

1. **San Bernardino access approach**: Should we use Playwright for SB (adds complexity and cost), or deprioritize SB until we find a simpler access method?

2. **Fixture count**: Is 25 fixtures sufficient, or should we invest in capturing 15+ more (mostly redundant) to hit the 40-80 target?

3. **OC Complex Civil case numbers**: Complex civil cases use different numbering. Should we add a JCCP-format regex to OC, or treat 0 case numbers as acceptable for CX departments?

---

## Artifacts

- `packages/scraper-framework/tests/fixtures/` — 25 fixture files
- `packages/scraper-framework/tests/fixtures/expected/` — 25 expected output JSON files
- `packages/scraper-framework/tests/fixtures/README.md` — fixture documentation
