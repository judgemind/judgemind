# San Francisco Tentative Ruling Endpoints Investigation

**Date:** March 4, 2026
**Issue:** [#9](https://github.com/judgemind/judgemind/issues/9)
**Status:** Complete

---

## Executive Summary

San Francisco Superior Court publishes tentative rulings through two distinct DLL-based web applications on `webapps.sftc.org`. All 8 civil endpoints (`tr.dll`) are now gated by **Cloudflare Turnstile CAPTCHA**, making simple HTTP scraping infeasible. However, the Family Law endpoint (`ufctr.dll`) has **no CAPTCHA** and returns a browsable HTML page with downloadable PDF ruling files — making it immediately scrapable with the existing PDF-link scraper template.

**Key finding:** The original CA county investigation noted CAPTCHA only on Probate. As of March 2026, **all** `tr.dll` endpoints are behind Cloudflare Turnstile. This changes the difficulty rating from "Easy" to "Medium (requires Playwright)."

---

## Endpoints Tested

### Civil Tentative Rulings (`tr.dll`) — ALL CAPTCHA-GATED

| RulingID | Department | Type | Status |
|----------|-----------|------|--------|
| 5 | Dept 304 | Asbestos Law & Motion | Cloudflare Turnstile |
| 6 | Dept 304 | Asbestos Discovery | Cloudflare Turnstile |
| 8 | Dept 304 | Asbestos Motion Calendar | Cloudflare Turnstile |
| 10 | Dept 301 | Law & Motion / Discovery | Cloudflare Turnstile |
| 2 | Dept 302 | Law & Motion / Discovery | Cloudflare Turnstile |
| 7 | (Probate) | Probate | Cloudflare Turnstile |
| 9 | Dept 210 | Real Property Housing Court | Cloudflare Turnstile |
| 3 | Dept 501 | Real Property Housing Court | Cloudflare Turnstile |

**Total:** 8 endpoints, covering 5 departments (301, 302, 304, 210, 501) and Probate.

**URL pattern:** `https://webapps.sftc.org/tr/tr.dll/?RulingID={N}`

**CAPTCHA details:**
- Type: Cloudflare Turnstile (not reCAPTCHA despite the CSS class name `g-recaptcha`)
- Sitekey: `0x4AAAAAAAhseTmUbrk0U5ab`
- Flow: Page loads with Turnstile widget → auto-submits form on completion → redirects to ruling content
- Form action: `?referrer=https://webapps.sftc.org/tr/tr.dll?RulingID={N}`

**Important:** HTTP URLs return 403 (Cloudflare blocks non-HTTPS). HTTPS is required.

### Family Law Tentative Rulings (`ufctr.dll`) — NO CAPTCHA

| Endpoint | Status | Content |
|----------|--------|---------|
| `https://webapps.sftc.org/ufctr/ufctr.dll` | 200 OK, no CAPTCHA | HTML page with PDF links |

**Departments covered:** 403, 404, 416

**Content structure:**
- HTML page listing PDF links organized by department and calendar day (Tuesday/Thursday)
- Each department has "Current" and "Previous Tentative Rulings" sections
- Previous rulings auto-deleted after 30 days
- PDF URL pattern: `https://webapps.sftc.org/ufctr/files/Dept%20{N}/Tentative%20Rulings/{Day}/{filename}.pdf`

**PDF format:**
- Standard court-generated PDFs, ~10–30 pages each
- Content includes: case number, hearing date/time, department, presiding judge, motion type, ruling text
- Example: `403 Tentative Rulings 3.03.2026.pdf` — 30 pages, 371 KB
- Text extractable with pdfplumber/pdfminer

### Dedicated Probate CAPTCHA URL

The SF court landing page links Probate through a dedicated CAPTCHA gateway:
`https://webapps.sftc.org/captcha/captcha.dll?referrer=https://webapps.sftc.org/tr/tr.dll?RulingID=7`

This page serves the identical Cloudflare Turnstile challenge as the direct `tr.dll` endpoints. As of March 2026, there is no functional difference — all `tr.dll` endpoints present the same CAPTCHA whether accessed directly or through the CAPTCHA gateway.

---

## Protocol Requirements

| Requirement | Value |
|-------------|-------|
| Protocol | HTTPS required (HTTP returns Cloudflare 403) |
| User-Agent | Standard browser UA works |
| Cookies | Not required for `ufctr.dll`; likely required after Turnstile solve for `tr.dll` |
| Rate limiting | Not observed (small number of endpoints) |
| Authentication | None beyond CAPTCHA |
| JavaScript | Required for Turnstile; not required for `ufctr.dll` |

---

## Response Format Analysis

### Family Law (ufctr.dll) — HTML with PDF Links

```html
<h2>Tentative Rulings - Unified Family Court</h2>
<!-- Instructions text -->
<table>
  <tr><th><H5>Dept 403 Tuesday-Court Calendar Tentative Rulings</H5></th></tr>
  <tr><td><a href="files/Dept 403/Tentative Rulings/Tuesday/403 Tentative Rulings 3.03.2026.pdf"
            target="_blank">403 Tentative Rulings 3.03.2026.pdf</a></td></tr>
</table>
<!-- Repeat for Thursday, Previous, other departments -->
```

Selectors for extraction:
- Department sections: `table` elements with `<H5>` headers containing dept number and day
- PDF links: `table td a[href$=".pdf"]`
- Department number: parse from header text (e.g., "Dept 403")
- Calendar day: parse from header text (e.g., "Tuesday-Court Calendar")
- Date: parse from filename (e.g., "3.03.2026")

### Civil (tr.dll) — Unknown Until CAPTCHA Solved

The actual ruling content behind the CAPTCHA has not been inspected. Based on the DLL architecture and the endpoint names, it likely returns rendered HTML with ruling text inline (not PDF links). This needs verification with Playwright.

---

## Recommended Implementation

### Phase A: Family Law Scraper (Easy, immediate)

**Approach:** Extend the existing PDF-link scraper template (Pattern 2).

1. Scrape `https://webapps.sftc.org/ufctr/ufctr.dll` for PDF links
2. Filter for current rulings (skip "Previous" sections, or capture all)
3. Download each PDF
4. Extract text with pdfplumber
5. Parse case number, hearing date, department, judge, ruling text

**Estimated effort:** Low — reuses existing PDF-link scraper infrastructure.

**Coverage:** 3 Family Law departments (403, 404, 416)

### Phase B: Civil Scraper with Playwright (Medium, requires investigation)

**Approach:** Use Playwright to navigate `tr.dll` endpoints and solve Cloudflare Turnstile.

1. Launch headless browser with Playwright
2. Navigate to each `tr.dll` endpoint
3. Wait for Cloudflare Turnstile to auto-solve (Turnstile is designed to pass legitimate browsers without user interaction in most cases)
4. Extract ruling content from the rendered page
5. Parse into structured data

**Estimated effort:** Medium — requires Playwright integration and Turnstile handling.

**Coverage:** 8 civil endpoints across 5 departments + Probate

**Risks:**
- Cloudflare Turnstile may not auto-solve in headless mode (may need `headless=False` or stealth mode)
- Turnstile challenge difficulty may increase with automated access patterns
- Need to verify actual content format behind CAPTCHA before building parser

### Phase C (Deferred): Historical Archive

The Family Law endpoint exposes "Previous Tentative Rulings" going back ~30 days. These should be captured on first run to build initial historical depth.

---

## Fixtures Saved

| File | Description | Size |
|------|-------------|------|
| `sf_family_law_page.html` | Family Law ufctr.dll index page | 8.7 KB |
| `sf_captcha_gate.html` | Cloudflare Turnstile CAPTCHA page from tr.dll | 2.4 KB |
| `sf_dept403_ruling.pdf` | Dept 403 Tuesday ruling PDF (March 3, 2026) | 371 KB |

All saved to `packages/scraper-framework/tests/fixtures/`.

---

## Updated Assessment vs. Original CA County Investigation

| Attribute | Original Assessment | Updated Assessment |
|-----------|-------------------|--------------------|
| Difficulty | Easy | **Easy (Family Law) / Medium (Civil, requires Playwright)** |
| CAPTCHA | Probate only | **All tr.dll endpoints** |
| Endpoints | ~7 civil | **8 civil (tr.dll) + 1 family law (ufctr.dll) = 9 total** |
| Content format | HTML (assumed) | **HTML + PDF links (Family Law confirmed); civil unknown** |
| Scraper type | Pattern 3 (Static Endpoint) | **Pattern 2 (PDF-link) for Family Law; Pattern 3 + Playwright for Civil** |

---

## Open Questions

1. **What does tr.dll content look like after solving Turnstile?** Need to verify with Playwright whether it returns inline HTML rulings or PDF links.
2. **Does Turnstile auto-solve in Playwright headless mode?** Cloudflare Turnstile is designed to be less intrusive than traditional CAPTCHAs, but headless detection may still block it.
3. **Is the Turnstile challenge temporary?** It may have been added recently as an anti-bot measure. Worth monitoring if it gets removed.
4. **Court holiday schedule impact?** Ruling PDFs reference specific hearing dates (Tuesday/Thursday for Family Law). Scraper should account for court holidays.
