# Test Fixtures

Real HTML pages and PDF files captured from live court websites for regression testing. Each fixture has a corresponding `expected/*.json` file documenting the expected parsed output.

**All fixtures were captured 2026-03-02** unless otherwise noted. Court website structure changes over time — if a test fails, the live site may have changed.

---

## Los Angeles County

**Pattern:** ASP.NET form (Pattern 1). GET the main page for dropdown + ViewState tokens, then POST per (courthouse, dept, date) combination. Ruling HTML returned in `div#speechSynthesis`.

| Fixture | What it tests |
|---------|--------------|
| `la_main_page.html` | Main page with 97-option dropdown. Tests: token extraction, dropdown parsing, option count, courthouse name extraction, future-date options (Pomona South). |
| `la_ruling_response.html` | POST response for ALH,3 (Alhambra Dept 3). **2 cases**, judge name present. Tests: multi-case parsing, judge name extraction, case number regex. |
| `la_ruling_bh205.html` | POST response for BH ,205 (Beverly Hills Dept 205). **2 cases**, future hearing date (03/03), judge name absent. Tests: trailing-space courthouse code, future dates. |
| `la_ruling_pas_p.html` | POST response for PAS,P (Pasadena Dept P). **1 case**, letter-only department code. Tests: alphanumeric dept codes. |
| `la_ruling_cha_f46.html` | POST response for CHA,F46 (Chatsworth Dept F46). **Same case number appears twice** (two motions). Tests: deduplication of case numbers. |
| `la_ruling_com_a.html` | POST response for COM,A (Compton Dept A). **1 case**. Tests: Compton courthouse, submission instructions in ruling text. |
| `la_ruling_smc49.html` | POST with stale ViewState for SMC,49. **Returns LA Court error page** (~8KB). Tests: error handling when option no longer exists (stale tokens). |
| `la_ruling_smc56.html` | Same as above — SMC,56. Another stale-token error response. |
| `la_ruling_smc1.html` | Same — SMC,1. Stale-token error response. |
| `la_ruling_van_a.html` | Same — VAN,A. Stale-token error (Van Nuys). |
| `la_ruling_tor_b.html` | Same — TOR,B. Stale-token error (Torrance). |
| `la_ruling_ea_h.html` | Same — EA,H. Stale-token error (Pomona South). |

### LA Edge Cases to Know

- **Stale ViewState**: The ASP.NET `__VIEWSTATE` token expires between sessions. If you POST with tokens from a previous GET, you get an LA Court error page (not a 4xx HTTP error — the server returns 200 with an error HTML). The scraper must detect this and not emit a "document captured" event. Pattern: response is ~8KB, no `div#speechSynthesis`, body contains "We're sorry".
- **Beverly Hills trailing space**: Courthouse code `"BH "` (with trailing space) appears in the dropdown option value. Must strip before comparison.
- **Same case, two rulings**: `la_ruling_cha_f46.html` shows case 21CHCV00539 appearing twice — two motions for the same case in one dept response. `case_count=2` (raw) but `unique_case_count=1`.
- **Judge name absent**: Only some courthouses include the "X Judge of the Superior Court" div. Beverly Hills, Pasadena, Chatsworth, Compton all have `judge_name=None` from the current parser.

---

## Orange County

**Pattern:** PDF link scraper (Pattern 2). GET index page → extract PDF links → download PDFs → pdfplumber text extraction → regex for case numbers.

**Case number format:** `DD-DDDDDDDD` (e.g., `25-01455183`)

| Fixture | What it tests |
|---------|--------------|
| `oc_civil_page.html` | Index page with 33 PDF links. Tests: link extraction, non-breaking space normalization, absolute URL resolution, courthouse mapping from dept code prefix. |
| `oc_apkarian_c25.pdf` | Central Justice Center, Dept C25 (Gassia Apkarian). **36 pages, 14 cases**, standard format. Primary regression fixture. |
| `oc_central_c34.pdf` | Central Justice Center, Dept C34 (H. Shaina Colover). **27 pages, 1 case**. Different header format ("Thursday at 1:30 PM"). |
| `oc_complex_cx.pdf` | Complex Civil Department, Dept CX101 (William D. Claster). **2 pages, 0 case numbers** (future date, complex case format). |
| `oc_north_n.pdf` | North Justice Center, Dept N6 (Julianne S. Bancroft). **26 pages, 0 case numbers matched** — known gap, different format. |
| `oc_west_w.pdf` | West Justice Center, Dept W15 (Richard Y. Lee). **38 pages, 10 cases**, standard format. |
| `oc_costa_mesa_cm.pdf` | Costa Mesa Justice Center, Dept CM02 (Andre De La Cruz). **33 pages, 0 case numbers matched** — known gap. |

### OC Edge Cases to Know

- **Non-breaking spaces in link text**: `"HOFFER, David A. - Dept  CX103"` contains `\xa0` (NBSP). Scraper normalizes to space.
- **Some PDFs on Pantheon CDN**: PDFs link to `live-jcc-oc.pantheonsite.io` rather than `occourts.org`. Both are valid.
- **Zero case numbers on N6 and CM02**: The `DD-DDDDDDDD` regex doesn't match all OC courts. CX and N departments may use different formats (complex case numbers, or numbers embedded differently in PDF). This is an open bug.
- **Stale dates**: `oc_complex_cx.pdf` has a future date (March 06, 2026 when captured March 02). OC posts some PDFs in advance.

### Courthouse Mapping (from dept code prefix)

| Prefix | Courthouse |
|--------|-----------|
| `CX*` | Complex Civil Department (Laguna Hills) |
| `CM*` | Costa Mesa Justice Center |
| `N*` | North Justice Center (Fullerton) |
| `W*` | West Justice Center (Westminster) |
| `C*` | Central Justice Center (Santa Ana) |

---

## Riverside County

**Pattern:** PDF link scraper (Pattern 2). Same framework as OC. SSL certificate issue: `verify_ssl=False` required.

**Case number format:** `CV{LOCATION}{YY}{NNNNNN}` (e.g., `CVPS2306157`)

| Fixture | What it tests |
|---------|--------------|
| `riv_page.html` | Index page with 17 PDF links. Tests: link extraction, Riverside link text format, judge name with suffix (III). **Note: many links are stale (2023/2024 PDFs).** |
| `riv_ps1.pdf` | Palm Springs Courthouse, Dept PS1 (Arthur Hester III). **4 pages, 4 cases**, current rulings. |
| `riv_hall_of_justice.pdf` | Hall of Justice, Dept 260. **1 page, 0 cases** — "No Tentative Rulings" placeholder (2023). Tests: old stale placeholder handling. |
| `riv_murrieta.pdf` | Murrieta Courthouse, Dept M205 (Belinda Handy). **1 page, 0 cases** — "No Tentative Rulings" for 2026-03-02. Tests: current-dated no-rulings response. |
| `riv_moreno_valley.pdf` | Moreno Valley Courthouse, Dept MV1 (David E. Gregory). **2 pages, 3 cases**, current rulings. |

### Riverside Edge Cases to Know

- **Stale PDFs on index page**: The Riverside index page links to very old PDFs (2023, 2024) for some departments. The scraper downloads whatever the link points to — old PDFs will have zero or few case numbers. This is expected behavior; the content hash will detect when a new PDF replaces an old one.
- **"No Tentative Rulings" PDFs**: Two types: (1) stale placeholder with no date (Dept 260), (2) current-dated "No Tentative Rulings" with today's date (M205). Both have 0 case numbers and that is correct — not an error.
- **Judge name suffixes**: "Arthur Hester III" — the suffix (III) must be included in the judge name.
- **Numbered departments**: Departments 01–15 (Hall of Justice in Riverside) have numeric-only codes.
- **SSL**: `verify=False` required — the site has a self-signed or misconfigured cert.

### Courthouse Mapping (from dept code prefix)

| Pattern | Courthouse |
|---------|-----------|
| `PS*` | Palm Springs Courthouse |
| `MV*` | Moreno Valley Courthouse |
| `M*` | Murrieta Courthouse |
| `C*` | Corona Courthouse |
| `01`–`15` (numeric) | Hall of Justice (Riverside) |

---

## San Bernardino County

**Status: BLOCKED — access denied to automated requests as of 2026-03-02.**

| Fixture | What it tests |
|---------|--------------|
| `sb_civil_page.html` | Access-denied response from SB civil tentative rulings URL. **No useful PDF links.** Documents the block behavior. |

### San Bernardino Findings

The URL `https://sanbernardino.courts.ca.gov/online-services/civil-tentative-rulings` returned an "Access denied" page to direct HTTP requests (200 OK status but with access-denied body). The page:
- Has only 1 PDF link: the phone roster (not a tentative ruling).
- Uses Google Tag Manager (JavaScript-based content loading is likely).
- Is a Drupal/Pantheon site (same platform as OC and Riverside, which do work).

**Hypothesis**: The SB page may use JavaScript-rendered content that the PDF-link scraper can't see, or the Pantheon CDN has bot protection configured for this specific court that OC/Riverside do not have.

**Next step**: See issue `#XX — [INVESTIGATE] San Bernardino tentative rulings access approach` for the follow-up investigation using Playwright.

---

## Expected Output Files

The `expected/` subdirectory contains JSON files documenting what the scraper should extract from each fixture. Keys:

- `_fixture`: the fixture file name
- `_description`: human-readable description
- `_captured`: date captured from live site
- `_edge_case`: if present, the category of edge case this fixture demonstrates
- `_notes`: additional context
- Parsed fields: `case_count`, `case_numbers`, `judge_name`, `courthouse`, `department`, `hearing_date`, `ruling_text_contains`

These are used as reference documentation, not directly as pytest assertions. Tests use the fixture files directly and assert against specific values from the expected JSON.

---

## Adding New Fixtures

When a scraper regression test is needed against a new court page or edge case:

1. Fetch the page/PDF with `httpx` (or Playwright for JS-rendered pages).
2. Save to `tests/fixtures/<court>_<description>.<ext>`.
3. Create a corresponding `tests/fixtures/expected/<court>_<description>.json`.
4. Write a regression test in the appropriate `tests/test_*.py` file using the `@pytest.mark.regression` marker.
5. Never commit large binary files over ~5MB to the repo — link to S3 instead.
