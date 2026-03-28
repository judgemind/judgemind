# Investigation: Judge Roster and Biographical Source Assessment

**Issue:** #2145
**Parent:** #2144
**Date:** March 2026
**Status:** Complete

---

## Executive Summary

We investigated all available sources of judge metadata and biographical information for our 13 active California counties plus statewide biographical sources. Key findings:

1. **Most counties publish roster data**, but formats vary widely -- from rich HTML tables (LA, OC, Kern, Fresno, Ventura) to PDF-only rosters (SF, San Bernardino) to no public roster at all (Sacramento).
2. **Ballotpedia covers approximately 67% of judges** based on our sampling (12/18 judges tested). Coverage is biased toward judges who have faced elections; recently appointed judges may not have pages. Trial court judge pages lack infoboxes but do contain education, career, and election data in structured sections.
3. **Governor's appointment press releases** are a rich, machine-readable source for career history and education. Published consistently at gov.ca.gov with regular cadence.
4. **California State Bar** provides bar admission date and law school, searchable by name. Requires form submission.
5. **No statewide centralized roster exists** -- the courts.ca.gov/judges.htm URL 404s. Each county maintains its own.

---

## Part 1 -- County Roster Pages

### Summary Table

| County | Roster URL | Format | Fields | Scrapability | Officers | Update Freq |
|--------|-----------|--------|--------|-------------|----------|-------------|
| **Los Angeles** | lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx | HTML table (class `joresultstable`) | Name, Title, Location, Dept, Phone, Primary Assignment, Litigation Areas | Plain HTTP, static HTML | 560 | ~Weekly |
| **Orange** | occourts.org/general-information/judicial-officers | HTML table | Judges, Panel, Floor, Dept, Phone | Plain HTTP, static HTML | 142 | ~Monthly (last: 03/20/2026) |
| **Riverside** | No dedicated roster page | PDF link text on tentative rulings page | Department, Judge Name (from link text) | Plain HTTP | ~30 depts | Daily (PDF links updated) |
| **Sacramento** | No public roster page found | N/A | N/A | N/A | N/A | N/A |
| **Alameda** | alameda.courts.ca.gov/general-information/judicial-directory-and-assignments | HTML table (phone/fax directory) | Department/Unit, Phone, Fax/Email | Plain HTTP | ~52 dept entries | ~Annually |
| **Kern** | kern.courts.ca.gov/general-information/judicial-officers | HTML tables (per courthouse) | Courtroom, Judge/Commissioner, Assignment | Plain HTTP, static HTML | ~50 officers | ~Annually |
| **San Bernardino** | sanbernardino.courts.ca.gov/system/files/general/schedassign.pdf | PDF | Judge, Department, Assignment, Courthouse | PDF extraction needed | ~50+ | Periodic |
| **Santa Clara** | santaclara.courts.ca.gov/general-information/judicial-information | HTML (courthouse-to-dept mapping only) + per-division pages | Dept, Judge Name (civil trial judges) | Plain HTTP | ~82 positions | ~Annually |
| **Ventura** | ventura.courts.ca.gov/judicial-assignments-* (3 pages) | HTML tables (class `tablesorter`) | Courtroom, Judicial Officer, Title, Calendar Type | Plain HTTP, static HTML | ~40 officers | ~Annually |
| **San Francisco** | sf.courts.ca.gov/general-information/judicial-assignments | PDF links (roster alphabetical + dept listing) | Judge, Department, Assignment | PDF extraction needed | ~50+ | ~Semi-annually |
| **Fresno** | fresno.courts.ca.gov/general-information/judicial-assignments | HTML tables (5 tables, per division) | Dept/Courthouse, Location, Judge/Commissioner, Phone, Assignment | Plain HTTP, static HTML | ~54 officers | ~Annually |
| **Contra Costa** | No dedicated roster page found | N/A (phone directory only, no judge names) | N/A | N/A | ~12 active civil depts | N/A |
| **San Diego** | sdcourt.ca.gov/sdcourt/generalinformation/judgeassignments | HTML table | Department + "Hon. Name" combined, PDF link | Plain HTTP | 25 civil/probate depts | ~Quarterly |

### Per-County Details

#### Los Angeles

**URL:** `https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx`

**Already scraped** -- we have `la_dept_judges.py` in production. This is the richest roster source with 560 officers and 7 columns: Name (Last, First), Title, Location (courthouse), Dept, Phone, Primary Assignment, Litigation Areas.

**Format:** Static HTML table with class `joresultstable`. Also supports legacy format (table id `GridView1`) for backward compatibility. No JavaScript required.

**Fields:** Name, Title (Judge/Commissioner), Courthouse, Department, Phone, Primary Assignment, Litigation Areas.

**Scrapability:** Excellent. Plain HTTP GET, no bot protection. Already implemented in `LACourtDirectory`.

---

#### Orange County

**URL:** `https://www.occourts.org/general-information/judicial-officers`

**Format:** Single HTML table with columns: Judges, Panel, Floor, Dept, Phone. 142 officers listed.

**Fields:** Judge full name (LAST, FIRST format), Panel (Harbor/West/Juvenile/etc.), Floor, Department code (H06, W16, L34, etc.), Phone.

**Department codes** encode the courthouse: C=Central, CCB=Community Court, H=HJC Newport Beach, L=Lamoreaux, N=North, W=West.

**Scrapability:** Excellent. Plain HTTP GET, static HTML table. No JavaScript required. Last updated date shown on page (03/20/2026).

**Note:** The sitemap uses `live-jcc-oc.pantheonsite.io` URLs (Pantheon hosting), but the production site at `occourts.org` serves the same content.

---

#### Riverside

**URL:** No dedicated judicial officer roster page exists.

**Existing approach:** The tentative rulings index page at `https://www.riverside.courts.ca.gov/online-services/tentative-rulings` contains PDF links with department-to-judge mappings embedded in the link text: "Department PS1 - Honorable Arthur Hester III". This is already scraped by `riverside_dept_judges.py`.

**Limitations:** Only covers departments that post tentative rulings. Departments without active rulings will not appear.

**Alternative:** The sitemap contains only news articles about new judge appointments, not a comprehensive roster. The court does not appear to publish a public judicial officer directory.

---

#### Sacramento

**URL:** No public roster page found.

**Investigation:** The saccourt.ca.gov site uses a legacy ASP.NET architecture (not JCC/Drupal). Every attempted URL pattern (/judges/*.aspx, /general/directory.aspx, etc.) returns a 404 or redirects to the error page. The homepage has a "Departments" link that appears to be a JavaScript dropdown with no static destination. There is no sitemap.xml. No judicial officer listing was found anywhere on the public-facing site.

**Scrapability:** Not feasible via HTTP. Would require Playwright to interact with JavaScript navigation, and even then the roster content may not exist on the site.

**Alternative approaches:** Governor's appointment press releases and Ballotpedia for Sacramento judges. Court filings or the county bar association may also maintain lists.

---

#### Alameda

**URL:** `https://www.alameda.courts.ca.gov/general-information/judicial-directory-and-assignments`

**Format:** Single HTML table, but it is a phone/fax directory organized by division (Civil, Criminal, Family, etc.), NOT a judge roster. Column headers: Department/Unit, Phone, Fax/Email. Judge names are NOT listed -- only department functions and contact numbers.

**Scrapability:** The page itself is plain HTTP, but it does not contain judge-to-department mappings. A separate source would be needed.

**Alternatives:** The PDF linked from the page (if any) may contain a full judicial assignments document. Otherwise, similar to Sacramento, judge names are not publicly listed on the website.

---

#### Kern

**URL:** `https://www.kern.courts.ca.gov/general-information/judicial-officers`

**Format:** Multiple HTML tables (9 total), one per courthouse division: Metropolitan Division (20 depts), Metropolitan Justice Building (11 divisions), Juvenile Justice Center (4 depts), Traffic Court (2 depts), Delano (3 depts), Shafter (2 depts), Lamont (2 depts), Mojave (3 depts), Ridgecrest (2 depts).

**Fields:** Courtroom, Judge/Commissioner, Assignment. No phone numbers.

**Scrapability:** Good. Plain HTTP, static HTML tables. The multi-table structure requires iterating over all tables, but each has consistent column headers (Courtroom, Judge/Commissioner, Assignment).

**Total officers:** ~50 judicial officers across all divisions.

---

#### San Bernardino

**URL:** `https://sanbernardino.courts.ca.gov/system/files/general/schedassign.pdf`

**Format:** PDF document (765 KB). The court does not have an HTML judicial officer page. The `judicial-governance` page returns a 403. The general information page links to a "Schedule of Assignments" PDF.

**Fields:** Expected to contain judge names, department assignments, courthouse locations.

**Scrapability:** Requires PDF text extraction. The PDF would need to be downloaded periodically and parsed. Not ideal but feasible.

**Note:** The sitemap uses `sanbernardino.lndo.site` URLs (Lando dev environment), which may indicate the site is in active development. The production site at `sanbernardino.courts.ca.gov` correctly serves the PDF.

---

#### Santa Clara

**URL:** `https://santaclara.courts.ca.gov/general-information/judicial-information`

**Format:** The main page has a courthouse-to-department range table (e.g., "Downtown Superior Court: Depts 1-16"), with links to individual courthouse pages. However, the individual courthouse pages (e.g., `/judicial-information/downtown-superior-court`) return 404.

**Civil Trial Judges** are separately listed at `/divisions/civil-division/civil-trial-judges` with a clean table: Department #, Trial Judge's Name (9 civil trial judges).

**Fields:** Department number, Judge name, (civil division only has links to standing orders).

**Scrapability:** Partial. The civil trial judges page is cleanly scrapable (9 rows, 2 columns). A full roster covering all 82 positions would require finding the individual courthouse pages or an alternative source.

**Total positions:** 77 judgeships + 5 commissioner positions = 82 total.

---

#### Ventura

**URL:** Three pages:
- `https://www.ventura.courts.ca.gov/judicial-assignments-ventura` (main, 29 rows)
- `https://www.ventura.courts.ca.gov/judicial-assignments-juvenile` (juvenile, 6 rows)
- `https://www.ventura.courts.ca.gov/judicial-assignments-east-county` (east county, 5 rows)

**Already scraped** -- we have `ventura_dept_judges.py` in production with `VenturaCourtDirectory`.

**Format:** HTML tables with class `tablesorter`. Columns: Courtroom, Judicial Officer, Title, Calendar Type.

**Fields:** Department number, Judge name (prefixed with "Hon."), Title (Judge/Commissioner), Calendar type/assignment.

**Scrapability:** Excellent. Plain HTTP, static HTML tables. Already implemented.

---

#### San Francisco

**URL:** `https://sf.courts.ca.gov/general-information/judicial-assignments`

**Format:** The page itself has no HTML tables or inline data. Instead, it provides links to PDF documents:
- **Judicial Roster Alphabetical List** (PDF): `/system/files/general/sftc-judges-roster-alpha-public-102025.pdf`
- **Judicial Roster Department Listing** (PDF): `/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf`
- Multiple **PJ Judicial Assignment** PDFs (dated versions)

**Fields:** Department, Judge name, Assignment (in PDFs).

**Scrapability:** Requires PDF extraction. The PDFs are publicly accessible via direct URL (no authentication). The dated filenames suggest they are updated semi-annually with amended versions.

**Note:** The JCC/Drupal site at sf.courts.ca.gov does not have a sitemap.xml.

---

#### Fresno

**URL:** `https://www.fresno.courts.ca.gov/general-information/judicial-assignments`

**Format:** 5 HTML tables organized by division/courthouse, totaling 54 officers:
- Main courthouse (28 departments)
- Domestic Violence (2 departments)
- Collaborative Courts/Traffic (5 departments)
- Family/Probate (15 departments)
- Juvenile (4 departments)

**Fields:** Department/Courthouse, Location, Judge/Commissioner, Phone Number, Assignment.

**Scrapability:** Excellent. Plain HTTP, static HTML tables. Rich data with phone numbers and specific assignment types. Consistent column structure across tables.

---

#### Contra Costa

**URL:** No dedicated roster page found.

**Investigation:** The court website at contracosta.courts.ca.gov has a phone directory page, but it lists only general department phone numbers, not judicial officer names. The sitemap shows no judicial officer directory page. A search for judge-related links found only news articles about new appointments.

**Existing data:** The tentative rulings page (from `cc_tentatives.py`) embeds judge names in department headers (e.g., "Department 09 - Judge Devine"), similar to Riverside.

**Alternative:** Governor's appointment press releases and the tentative rulings page for active civil judges.

---

#### San Diego

**URL:** `https://www.sdcourt.ca.gov/sdcourt/generalinformation/judgeassignments`

**Format:** Single HTML table with 25 rows. Each row combines department and judge name in a single cell (e.g., "Department N-18Hon. Renee N.G. Stackhouse") with a link to a PDF of department policies.

**Fields:** Department code (N-18, N-27, 60, 62, etc.), Judge name ("Hon. First Last" format), PDF link to department policies.

**Scrapability:** Good but requires text parsing. The department and judge name are concatenated in a single `<td>` cell without a separator. A regex like `Department\s+(\S+)\s*Hon\.\s*(.+)` can extract the components. Only lists 25 departments (civil/probate assignments) -- not the full court roster.

---

## Part 2 -- Biographical Sources (Statewide)

### Ballotpedia

**URL pattern:** `https://ballotpedia.org/{First}_{Middle_Initial}_{Last}` (various formats)

**Coverage:** Approximately **67% of CA trial court judges** based on our 18-judge sample across all 13 counties:
- **12/18 tested judges had pages** (67%)
- LA County had 7/7 coverage (100%), but this is likely biased -- LA judges face competitive elections more frequently
- Several smaller counties (Kern, CC, Ventura, Alameda, Riverside) had 0/1 from our limited sample
- Coverage is biased toward judges who have **faced contested elections** -- appointed judges who haven't yet appeared on a ballot may not have pages

**Fields available on trial court judge pages:**
- Education (undergraduate institution + law school, sometimes with graduation years)
- Career history (prior positions, law firms, government roles)
- Election history (year, opponent, vote counts, percentage)
- Campaign themes
- Some pages include a Biography section with appointment details
- **No infobox** on trial court judge pages (unlike appellate/federal judges, which have structured infoboxes)

**Scrapability:** Pages are public HTML served by MediaWiki. The structured sections (Education, Career, Elections) follow a consistent heading pattern that can be parsed. However:
- Name format in URLs is inconsistent (may include or omit middle initial)
- 404s for non-existent pages are clean (just a "does not have an article" message)
- No obvious rate limiting on individual page fetches
- No API for batch lookup -- each judge requires a separate page fetch

**Estimated coverage for 264 judges:** ~175 judges (67%), though actual coverage may be lower for recently appointed judges.

### Governor's Appointment Press Releases

**URL:** `https://www.gov.ca.gov/?s=judicial+appointment` (search), or individual press releases at `https://www.gov.ca.gov/YYYY/MM/DD/...`

**Format:** Each press release is a dedicated web page with structured paragraphs per appointee.

**Sample format (from 3/27/2026 press release):**
> "Sarvenaz Bahar, of Los Angeles County, has been appointed to serve as a Judge in the Los Angeles County Superior Court. Bahar has served as a Deputy District Attorney at the Los Angeles County District Attorney's Office since 2007..."

**Fields per appointee:**
- Full name
- County of residence
- Court appointed to
- Current/most recent position + employer + dates
- Previous positions (multiple entries)
- Education (law school + undergraduate, sometimes with degrees)
- Bar admission (implicit from career dates)

**Cadence:** Multiple press releases per month (10+ in the last 7 months). Each release covers 5-20 appointments across multiple counties.

**Scrapability:** Excellent. The search page at gov.ca.gov returns paginated results. Individual press releases are public HTML with consistent paragraph structure. The biographical text follows a reliable template that can be parsed with regex or LLM extraction.

**Limitation:** Only covers **newly appointed judges**. Does not cover judges who won their seats via election without gubernatorial appointment. However, since most CA judges are initially appointed (and then stand for retention), this covers the majority over time.

### California State Bar

**URL:** `https://apps.calbar.ca.gov/attorney/Licensee/Search`

**Format:** Search form with fields: FreeText, LastName, FirstName, MiddleName, FirmName, City, Zip, PracticeArea.

**Fields available (from attorney profiles):**
- Bar number
- Name
- Status (active/inactive)
- Admission date
- Law school
- Address (city/state)
- Practice areas

**Scrapability:** Requires form submission (POST request). Individual attorney profiles are accessible via direct URL pattern: `https://apps.calbar.ca.gov/attorney/Licensee/Detail/{bar_number}`. However:
- Need to know the bar number or search by name
- Judges who have moved to the bench may have inactive bar status
- No batch API
- Possible rate limiting on searches

**Value:** Provides bar admission date and law school -- useful data points that complement Ballotpedia and governor's press releases.

### California Courts Statewide Roster

**URL tested:** `https://www.courts.ca.gov/judges.htm` (404)

**Finding:** No statewide centralized roster exists. The `courts.ca.gov` website links to individual county court websites but does not aggregate judicial officer data. The "Superior Courts" page (`courts.ca.gov/courts/superior-courts`) lists links to each county's website but has no judge data.

The Ballotpedia article "California Superior Courts" also does not provide a roster -- it covers judicial selection process, qualifications, and court structure.

**Conclusion:** There is no single source for all CA trial court judges. The roster must be assembled county-by-county.

### Governor's Appointment Data Release

**URL:** `https://www.gov.ca.gov/2026/02/27/governor-newsom-releases-2025-judicial-appointment-data/`

**Finding:** The governor's office publishes annual aggregate appointment data (demographics, gender, prior roles). This is useful for analytics but does not provide a searchable roster.

---

## Scrapability Summary by Approach

### Tier 1: Already Scraped (2 counties)
These counties already have `dept_judges.py` implementations with `CourtDirectory` integration:
- **Los Angeles** -- `la_dept_judges.py` (HTML table, 560 officers)
- **Ventura** -- `ventura_dept_judges.py` (3 HTML tables, ~40 officers)

### Tier 2: Clean HTML Tables -- Easy (4 counties)
These counties publish judicial officer data in static HTML tables scrapable with plain httpx + BeautifulSoup:
- **Orange County** -- Single table, 142 officers, excellent structure
- **Kern** -- 9 tables (one per courthouse), ~50 officers, consistent format
- **Fresno** -- 5 tables (one per division), 54 officers, rich data (phone + assignment)
- **San Diego** -- Single table, 25 civil/probate depts, needs regex to split dept+name

### Tier 3: PDF Extraction Required (2 counties)
These counties only publish roster data as PDF documents:
- **San Francisco** -- Department listing PDF, updated semi-annually
- **San Bernardino** -- Schedule of Assignments PDF, periodic updates

### Tier 4: Partial Data Only (3 counties)
These counties have roster data but it is incomplete or in a non-ideal format:
- **Riverside** -- Judge names only from tentative rulings link text (already scraped)
- **Contra Costa** -- Judge names only from tentative rulings department headers
- **Santa Clara** -- Only civil trial judges page available (9 of 82 positions); full roster behind 404 links

### Tier 5: No Public Roster (2 counties)
These counties do not publish a public judicial officer listing:
- **Sacramento** -- Legacy ASP.NET site with no discoverable roster page
- **Alameda** -- Only a phone directory (no judge names)

---

## Recommended Implementation Plan

### Phase 1: Roster Scraper for Tier 2 Counties

Build `CourtDirectory` implementations for the 4 counties with clean HTML tables. These can share a common `HtmlTableDirectoryScraper` base since the pattern is identical: fetch page -> find table -> extract (dept, judge, assignment).

**Issue scope:** Single implementation issue covering OC, Kern, Fresno, and San Diego.

### Phase 2: PDF Roster Extraction for Tier 3 Counties

Build PDF extraction for SF and San Bernardino roster documents. These will need periodic re-fetching (semi-annual/quarterly) and PDF-to-text parsing.

### Phase 3: Gap-fill for Tier 4 & 5 Counties

For counties with no roster page (Sacramento, Alameda) or only partial data (Santa Clara), rely on:
1. Governor's appointment press releases (scraped statewide)
2. Ballotpedia pages (for ~67% of judges)
3. Existing tentative rulings link text (Riverside, Contra Costa)

### Phase 4: Biographical Source Integration

Build scrapers for cross-county biographical sources:
1. **Governor's appointment press releases** -- highest value, structured format, covers most judges
2. **Ballotpedia** -- ~67% coverage, education + career + election history
3. **California State Bar** -- bar admission date + law school (supplementary)

---

## References

- `packages/scraper-framework/src/courts/ca/la_dept_judges.py` -- existing LA implementation
- `packages/scraper-framework/src/courts/ca/ventura_dept_judges.py` -- existing Ventura implementation
- `packages/scraper-framework/src/courts/ca/riverside_dept_judges.py` -- existing Riverside implementation
- `packages/scraper-framework/src/framework/court_directory.py` -- CourtDirectory base class
- Issue #2144 -- parent epic for judge bio/roster enrichment
