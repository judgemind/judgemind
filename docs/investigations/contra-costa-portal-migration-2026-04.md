# Contra Costa Tentative Rulings Portal Migration Investigation

**Date:** April 17, 2026
**Issue:** [#2601](https://github.com/judgemind/judgemind/issues/2601)
**Status:** Complete — findings documented, follow-up sub-tasks filed

---

## Executive Summary

Contra Costa County Superior Court is partially through a migration from the legacy `retired.cc-courts.org` ASP.NET site to a Drupal-based portal at `contracosta.courts.ca.gov`. The new portal **does support enumerable browsing** — filtering the `/tentative-rulings` Views endpoint by `field_judge_target_id` returns a per-judge listing table of tentative rulings, each linking to a structured detail page. The detail page exposes **more extracted fields than the existing PDF scraper produces** (case number, case type, hearing date/time, nature of proceedings, ruling text as HTML, and the linked PDF).

**Migration is viable**, not a blocker. The recommended path is a new scraper targeting the Drupal Views endpoint, dual-run during the cutover, and a gradual retire of the existing `retired.cc-courts.org` scraper.

**Critical caveat:** As of 2026-04-17 the production page at `/online-services/tentative-rulings` **still iframes the retired site** — the retired URL is still the authoritative source for all 10 current departments. The new portal is actively being populated but is not yet the primary publication channel, and some judges/departments are still missing. We should not prematurely cut the retired scraper; we should build the new scraper and dual-run until the new portal has full coverage.

---

## Current scraper state (retired.cc-courts.org)

- Module: `packages/scraper-framework/src/courts/ca/cc_tentatives.py`
- Index URL: `https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx`
- Pattern: HTML index with `<a class='tentative-ruling'>` links; backslash paths like `TR\Department 16 - Judge Reyes\16_031126.pdf`
- Departments captured (10): 09 (Devine), 10 (Campins), 14 (Athanasiou), 16 (Reyes), 18 (Douglas), 20 (OConnell), 30 (George - Probate), 32 (Hiramoto), 38 (Hinton - Probate), 39 (Weil)
- Behavior: takes only the **most recent** PDF per department (not the full history)
- LLM split path: enabled via `ENABLE_CC_LLM_EXTRACTION` — PDFs are multi-case, split by LLM

## New portal discovery

### Page layout

| URL | Purpose | Findings |
|---|---|---|
| `/online-services/tentative-rulings` | Public-facing page | **Still iframes the retired `motions-hearings-tentative.aspx`** (as of 2026-04-17). Describes department reassignments and phone fallback. |
| `/test-page-tentative-rulings` | Prototype search form | Exposes the real form that POST/GETs to `/tentative-rulings` with `case_number` + `field_judge_target_id`. |
| `/tentative-rulings` | Drupal Views endpoint (GET) | Accepts `case_number` and/or `field_judge_target_id` query params. Returns HTML table of matching rulings. Empty filter → validation error ("Enter a case number or select a Judicial Officer"). |
| `/tentative-ruling/<slug>` | Detail page per ruling | Structured HTML with Case Number, Case Type, Hearing Date/Time, Nature of Proceedings, ruling body (HTML), PDF link, and judge aside. Slug is lowercased case number (e.g. `/tentative-ruling/l24-04564`). Duplicate slugs get `-0`, `-1` suffixes. |
| `/tentative-rulings-archive` | Archive page | Currently empty ("No older Tentative Rulings were found at this time."). |
| `/sitemap.xml` | Drupal sitemap | Lists route pages but **not individual `/tentative-ruling/<slug>` nodes** — enumerating slugs requires paging the Views endpoint. |

### Judicial officer dropdown (authoritative IDs from the form)

| `field_judge_target_id` | Name | Maps to retired dept? |
|---|---|---|
| 238 | JOHN P DEVINE | Dept 09 (+34 per reassignment 1/5/26) |
| 242 | CHARLES S TREAT | Not on retired (new or recently assigned) |
| 245 | BENJAMIN REYES | Dept 16 |
| 276 | DANIELLE K DOUGLAS | Dept 18 (reassigned to Dept 32 per 1/5/26 notice, but Dept 32 on retired is Hiramoto — Douglas may be elsewhere now) |
| 278 | SHARA E BELTRAMO | Not on retired (new or recently assigned) |
| 280 | EDWARD G WEIL | Dept 39 (reassigning civil to Dept 10 per 3/2/26 notice) |

### Coverage gap on new portal

Judges on retired but **not in the new portal dropdown** (as of 2026-04-17):

- Campins (Dept 10)
- Athanasiou (Dept 14)
- OConnell (Dept 20)
- Hiramoto (Dept 32) — note: the cutover memo says Dept 18 → Dept 32, but Hiramoto is not in the new portal's list at all
- George (Dept 30 Probate)
- Hinton (Dept 38 Probate)

### Result table structure (per-judge listing)

Filter example: `GET /tentative-rulings?field_judge_target_id=245` (Reyes)

Returns HTML table:

```html
<table class="cols-2">
  <thead>
    <tr>
      <th class="views-field views-field-field-date-time">Date</th>
      <th class="views-field views-field-title">Case</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><time datetime="2025-01-29T16:31:00Z">Wednesday, January 29, 2025 - 08:31</time></td>
      <td>
        <a href="/tentative-ruling/l24-04564">L24-04564</a>
        <p>SCOTT FUGERE VS. THE COUNTY OF CONTRA COSTA</p>
        Civil<p>CASE MANAGEMENT CONFERENCE</p>
      </td>
    </tr>
  </tbody>
</table>
```

The table row contains: hearing date+time (ISO8601 in `datetime` attr), slug link, case number (link text), case title (first `<p>`), case type (text after title), nature of proceedings / motion type (second `<p>`).

### Detail page structure

```html
<article role="article" about="/tentative-ruling/l24-04564">
  <h1>Tentative Ruling: SCOTT FUGERE VS. THE COUNTY OF CONTRA COSTA</h1>
  <h2>Case Number</h2>
  <p><span>L24-04564</span></p>
  <h2>Case Type</h2>
  <p><div>Civil</div></p>
  <h2>Hearing Date / Time</h2>
  <p>Wed, 01/29/2025 - 08:31</p>
  <h2>Nature of Proceedings</h2>
  <p>CASE MANAGEMENT CONFERENCE</p>
  <h2>Tentative Ruling</h2>
  <p>
    <a href="/system/files/general/16_012925.pdf">Tentative Ruling PDF</a>
    <p>Before the Court are a demurrer and motion to strike...</p>
    <p>[additional paragraphs of ruling text]</p>
  </p>
  <aside class="jcc-body__aside">
    <h3 class="jcc-body__aside-title">Judges</h3>
    <h4>BENJAMIN REYES</h4>
  </aside>
</article>
```

Notable properties of the detail page:

1. **Ruling text available as HTML** (no PDF extraction needed). Equivalent to or better than LA/SF HTML scrapers.
2. **PDF still linked** — good for fallback and raw capture to S3 (scraper-first priority: capture raw to S3).
3. **PDF filename still follows retired pattern** — e.g. `16_012925.pdf` uses the same `DD_MMDDYY.pdf` convention (dept_date.pdf). PDFs live at `/system/files/general/<filename>.pdf`.
4. **No explicit department number field** — department is only implicit (via judge→dept mapping) or derivable from PDF filename prefix.
5. **Judges aside** often matches the dropdown name, but whitespace/casing varies (e.g. `"BENJAMIN REYES"` vs. `"BENJAMIN T REYES II"` on retired PDF header).
6. **Test data is still present** — Devine's listing includes entries `/tentative-ruling/test-case`, `/tentative-ruling/test-1`. The new scraper should filter test entries (e.g. skip slugs matching `^test` or where case_number looks invalid).

### URL parameters probed

| Query | Result |
|---|---|
| `?field_judge_target_id=238` (Devine) | 7 rulings including 3 test entries |
| `?field_judge_target_id=242` (Treat) | 1 ruling (C22-01620 JOHN DOE VS. CLINIVATE LLC) |
| `?field_judge_target_id=245` (Reyes) | 1 ruling (L24-04564) |
| `?field_judge_target_id=276` (Douglas) | 1 ruling (C22-01081 WINEHAVEN LEGACY LLC VS. CITY OF RICHMOND) |
| `?field_judge_target_id=278` (Beltramo) | 1 ruling (test) |
| `?field_judge_target_id=280` (Weil) | 1 ruling (MSN23-2201 SANDIA PEARSON VS MORAGA-ORINDA FIRE DISTRICT) |
| `?case_number=C22-01620` | Works — returns rows matching case number (empty table when no match) |
| `?_format=json` | HTTP 406 — Drupal REST JSON not exposed for Views (expected) |
| no params | HTTP 200 with error alert "Enter a case number or select a Judicial Officer." |

Total rulings enumerable today: ~12 across all 6 judges (mostly stale: 2024-2025 dates, with several test entries). **The new portal is not yet the authoritative publication channel** — the retired site still has fresh daily rulings for all 10 departments.

### PDF retrieval

PDF URLs on the new portal: `/system/files/general/<filename>.pdf` (all files live in a flat `general` folder rather than per-department directories). The filename convention (`16_012925.pdf` = dept 16, 01/29/25) is preserved from the retired site, so existing filename parsing helpers (`_cc_hearing_date_from_filename`, department prefix) continue to work if we fall back to PDFs.

Both hosts serve PDFs today:
- Retired: `https://retired.cc-courts.org/civil/TR/Department%2016%20-%20Judge%20Reyes/16_041526.pdf` → 200 OK, 589 KB
- New: `https://contracosta.courts.ca.gov/system/files/general/16_012925.pdf` → 200 OK, 444 KB

---

## Migration plan

Because the retired site is still the authoritative source and the new portal's coverage is incomplete, the safe path is:

### Phase 1 — Build parallel scraper (now)

Implement `cc_tentatives_portal.py` (new scraper class) targeting the Drupal Views endpoint. Run it **alongside** the existing scraper, writing to the same S3 bucket but with a distinct `scraper_id` (e.g. `ca-cc-tentatives-portal`). Compare capture counts daily.

Key behaviors:

- Iterate every `field_judge_target_id` from the known dropdown (start with the 6 discovered; periodically re-fetch the form to detect new IDs).
- For each judge, fetch `/tentative-rulings?field_judge_target_id=<id>`, parse the result table.
- For each row:
  - Extract case number, hearing date (ISO from `<time datetime=...>`), case title, case type, motion type directly from the table row (these are scraper-provided metadata, not LLM-extracted).
  - Fetch the detail page `/tentative-ruling/<slug>` — pull the ruling body HTML and the linked PDF URL.
  - Download the PDF (raw capture to S3 — this is what makes the rulings durable).
  - Skip test entries (slug matches `^test` or case number doesn't match `^[CLNP]\d{2}-\d{4,5}$` or `^MSN\d{2}-\d{4,5}$`).
- Use `ContentFormat.HTML` for the new scraper (the ruling body is HTML; PDF is a parallel artifact). Alternatively, capture both and use the PDF as primary raw to keep the pipeline consistent with today's CC flow.
- Archive both the detail page HTML and the PDF to S3 so we have both representations.

### Phase 2 — Dual-run validation

For at least two weeks (or until we see the retired site drop a department that the new portal captures), dual-run and produce a daily diff:

- Count captures per department per day on each scraper.
- Compare case numbers / dates to find rulings the old scraper caught but the new one missed and vice versa.
- Flag any divergence via `telemetry.scraper_runs` metrics.

### Phase 3 — Cutover

Once the new scraper captures at least the union of what the retired scraper captures (plus any new judges Treat/Beltramo post to), flip the production scraper and deprecate the retired one. Keep the retired scraper code as a fallback path triggered by env flag.

### Phase 4 — Department mapping update

Apply the announced reassignments to `derived.courts`:

- Dept 9 civil → Dept 34 (Martinez) [eff. 1/5/26]
- Dept 18 civil → Dept 32 [eff. 1/5/26]
- Dept 34 limited civil (Richmond) → Dept 14 (Richmond) [eff. 1/5/26]
- Dept 39 civil → Dept 10 [eff. 3/2/26]

This is a data-migration task, best done as a rebuild (`rebuild_db.py --county "Contra Costa"`) once the new scraper's data lands.

---

## Stale claims in source docstrings (B.1.5 compliance)

`packages/scraper-framework/src/courts/ca/cc_tentatives.py` lines 1–45 docstring currently documents the retired-site HTML structure and asserts "404 PDF links found on index page (across ~11 active departments)". This remains accurate for the retired site today but will become stale on cutover. The docstring change is bundled into the Phase 3 follow-up (new scraper module) and the Phase 1 follow-up (parallel scraper) — both explicitly call out adding a docstring cross-reference.

No existing docstring is immediately contradicted today (the retired site still works exactly as described). Follow-up issues carry explicit text to update the docstring when they land.

---

## Follow-up issues

Filed as sub-tasks of #2601. Each is independently pickup-able.

- **[follow-up 1 — Phase 1 scraper]** Implement `cc_tentatives_portal.py` scraper targeting `/tentative-rulings` with per-judge enumeration. Details in the filed issue.
- **[follow-up 2 — Phase 2 dual-run]** Add dual-run orchestration: both `ca-cc-tentatives` and `ca-cc-tentatives-portal` run in parallel, with a daily diff report.
- **[follow-up 3 — Phase 3 cutover]** Retire the `retired.cc-courts.org` scraper once new portal has full coverage. Keep behind an env flag for rollback.
- **[follow-up 4 — Department reassignment data migration]** Update `derived.courts` to reflect the announced dept reassignments; rebuild affected docs.
- **[follow-up 5 — Fixture capture]** Capture fresh fixtures from the new portal (listing page, detail page, PDF) for regression tests.
- **[follow-up 6 — Monitor the retired site]** Add a lightweight check that alerts if `retired.cc-courts.org` starts returning redirects to the new portal or 404s on the index page — an early sunset signal.

---

## Appendix — commands used

Discovery commands (ran from the agent worktree):

```bash
curl -sL "https://contracosta.courts.ca.gov/online-services/tentative-rulings"    # production page (iframes retired site)
curl -sL "https://contracosta.courts.ca.gov/test-page-tentative-rulings"          # prototype form
curl -sL "https://contracosta.courts.ca.gov/tentative-rulings"                    # empty filter → validation error
curl -sL "https://contracosta.courts.ca.gov/tentative-rulings?field_judge_target_id=245"  # Reyes listing
curl -sL "https://contracosta.courts.ca.gov/tentative-ruling/l24-04564"           # detail page
curl -sL "https://contracosta.courts.ca.gov/system/files/general/16_012925.pdf"   # PDF download
curl -sL "https://contracosta.courts.ca.gov/sitemap.xml"                          # sitemap (no detail nodes)
```
