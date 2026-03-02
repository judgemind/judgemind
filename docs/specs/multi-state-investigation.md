# Judgemind Multi-State Court Data Investigation

**March 2026**

*Companion to: CA County Tentative Rulings Investigation*

---

## Executive Summary

This investigation examines the court data landscape across Judgemind's five priority states (CA, TX, FL, NY, IL) and the broader national ecosystem. The findings fundamentally reshape the product strategy:

**The single most important finding: Tentative rulings are a California-only institution.** Trellis's own support documentation confirms this explicitly. The entire tentative ruling capture pipeline — the core of our CA strategy — has zero applicability outside California. In other states, judge analytics must be derived entirely from docket entries, filed orders, and court documents.

This means Judgemind is actually building **two different products**: a tentative-ruling-powered judge intelligence platform for California, and a docket/document-based analytics platform for everywhere else. The architecture must accommodate both.

---

## State-by-State Analysis

### 1. California — Tentative Ruling State (Unique)

**Court System Structure:**
- 58 counties, each with a Superior Court
- Trial court of general jurisdiction
- No statewide unified CMS — courts use a mix of Tyler Odyssey, custom systems, and various vendors

**What's Publicly Available:**
- **Tentative rulings** (the unique data asset): Pre-hearing judicial decisions posted 1-2 days before hearing, ephemeral
- **Dockets:** Via individual county clerk websites, some via Odyssey portals
- **Documents:** Filed pleadings, motions, orders — availability varies by county
- **Calendars:** Hearing schedules, often on court websites

**Data Access Architecture:**
- Fragmented across 58 county systems
- 6 distinct publication patterns identified (see CA investigation report)
- Tyler Odyssey backend in 28+ counties but tentative publication is decoupled from CMS
- No statewide search portal for trial court data
- Tyler re:SearchCA exists but focused on e-filed documents, not tentatives

**Judge Analytics Path:**
- Tentative rulings are the primary data source — they explicitly state how a judge rules on specific motions
- Tentatives contain structured data: case number, motion type, outcome (grant/deny/partial), reasoning
- 25,000+ tentative rulings per year across the state (per tentativerulings.org estimates)
- Rich analytics possible: grant/deny rates by motion type, by judge, reasoning patterns

**Key Competitors:**
- tentativerulings.org — 25 CA counties, ~200 judges, paid subscription
- rulings.law — CA focused, free (ad-supported), ~180 judge folders
- Bench Reporter — primarily LA County, $39/mo, crowdsourced tagging

**Priority:** Highest. Our architectural knowledge gives us the fastest path to value here.

---

### 2. Texas — Centralized Statewide System

**Court System Structure:**
- 254 counties
- 457 District Courts (primary trial courts)
- County Courts, County Courts at Law, Justice Courts, Municipal Courts
- Two supreme courts: Supreme Court of Texas (civil), Court of Criminal Appeals (criminal)

**What's Publicly Available:**
- **re:SearchTX** — Tyler Technologies statewide portal covering all 254 counties
  - URL: research.txcourts.gov
  - Civil, family, and probate case information
  - Licensed attorneys: documents back to 2016
  - Registered public users: documents from 2018 to present
  - Free basic search; premium/pro tiers for advanced features (full-text search, alerts)
  - Powered by eFileTexas e-filing system
- **County-level portals** — Larger counties (Harris, Dallas, Tarrant, Travis, Bexar) also have independent online docket search systems
- **No tentative rulings** — Texas does not use a tentative ruling procedure at the trial court level

**Data Access Architecture:**
- **Centralized advantage:** re:SearchTX is effectively a single API endpoint for all 254 counties
- Built on Tyler Technologies' e-filing infrastructure
- Anti-scraping provisions in Terms of Service: prohibits "bulk copying or bulk distribution" and storing in "searchable database accessible to third parties" without permission
- Individual county clerk portals vary: Harris County (hcdistrictclerk.com) requires registration, offers free viewing but reserves rights on bulk access
- TAMES (Texas Appeals Management and eFiling System) covers appellate courts

**Judge Analytics Path (Without Tentative Rulings):**
- Must derive from **docket entries and filed documents**
- Court orders filed in cases contain ruling outcomes (granted/denied)
- Docket text entries summarize hearing results
- NLP/AI extraction needed: parse docket entries for motion type + outcome
- Lower signal density than CA tentative rulings — orders may be terse, not all outcomes clearly recorded
- Higher volume partially compensates: Texas has massive case volume

**TOS Considerations:**
- re:SearchTX TOS explicitly restricts bulk downloading and redistribution
- Individual county clerk portals have their own terms
- May need to approach Texas OCA directly for partnership or data access
- Alternative: scrape individual county clerk portals that have more permissive terms

**Priority:** High. Second largest state by population and litigation volume.

---

### 3. Florida — Fragmented County Systems

**Court System Structure:**
- 67 counties, 20 judicial circuits
- Circuit Courts (general trial courts), County Courts (limited jurisdiction)
- 67 independently elected Clerks of Court and Comptrollers

**What's Publicly Available:**
- **No statewide case search portal** for trial courts
- **Florida Courts E-Filing Portal** (myflcourtaccess.com) — statewide e-filing, but primarily for filing, not public search
- **CCIS (Comprehensive Case Information System)** — statewide aggregation, but restricted access (governmental agencies, not general public search)
- **Individual county clerk portals** — Each of 67 counties has their own online records system
  - Some are Tyler Odyssey-based
  - Some use Civitek (ClericusCMS)
  - Some use other vendors
  - Access levels governed by FL Supreme Court administrative orders (AOSC24-65)
- **Access tiers** by user role: public gets limited view; registered users see more; attorneys of record get full access
- **Document review bottleneck:** In some counties (e.g., Duval/Jacksonville), documents must be reviewed by clerk staff before being published online

**Data Access Architecture:**
- **Most fragmented** of the five priority states
- No tentative rulings
- No equivalent to re:SearchTX — must build 67 individual county scrapers
- Some shared CMS platforms (Civitek, Tyler) could enable template-based scraping
- Florida Supreme Court Standards for Electronic Access create a security matrix governing what's visible to whom
- Some counties charge per-page fees for document access ($2.50 first page + $1/additional page in some jurisdictions)

**Judge Analytics Path:**
- Derived from docket entries and filed orders/judgments
- Court orders are the primary source for ruling outcomes
- Florida's circuit-based system means judges rotate — tracking requires entity resolution across circuits
- Some counties have significantly better online access than others
- Document availability varies widely — some counties have documents from 2008+, others are more limited

**Key Challenges:**
- 67 separate scraper targets (largest county count of any priority state relative to what must be individually addressed)
- Varying CMS platforms across counties
- Per-page document fees in some jurisdictions
- Document review/redaction delays before publication
- Security matrix restrictions on remote access

**Priority:** Medium-high. Third largest state but highest scraping complexity.

---

### 4. New York — Unified Court System with Multiple Portals

**Court System Structure:**
- 62 counties
- Supreme Court (general trial court — counterintuitively named)
- County Courts, City Courts, District Courts, Town/Village Justice Courts
- Court of Claims
- Family Court
- NYC has its own Civil Court and Criminal Court
- Unified Court System (UCS) administered by Office of Court Administration

**What's Publicly Available:**
- **NYSCEF (New York State Courts Electronic Filing)** — e-filing system with public case search
  - iapps.courts.state.ny.us/nyscef/
  - Search as guest (no account needed)
  - Supreme Court civil cases in many counties
  - Court of Claims cases
  - Some Family Court cases
  - Full document access for e-filed cases
  - "Search as Guest" feature allows anonymous browsing
- **eCourts** — Docket and case information portal
  - iapps.courts.state.ny.us/webcivil/ecourtsMain
  - Civil Supreme Court cases (all 62 counties, 1998-present)
  - Local Civil Court cases
  - Criminal cases (participating courts)
  - Family Court cases (participating courts)
  - eTrack: email alerts for case updates
- **WebCivil Supreme** — Supreme Court civil case dockets (62 counties)
- **WebCivil Local** — Local civil court cases
- **WebCriminal** — Criminal case information (participating courts)

**Data Access Architecture:**
- **Semi-centralized:** UCS provides several statewide portals
- NYSCEF covers e-filed documents — growing coverage as e-filing becomes mandatory
- eCourts provides docket/case information across courts
- Both systems are web-accessible with structured URLs
- No tentative rulings
- Third-party services like CourtAlert offer NYSCEF monitoring ($1.50/day/case)

**Judge Analytics Path:**
- Derived from docket entries, filed orders, and decisions available through NYSCEF
- WebCivil Supreme includes some decisions for some cases
- NYSCEF contains full documents including court orders
- NY Supreme Court (trial level) decisions are sometimes published on court websites
- Entity resolution: judges go by "Justice" in Supreme Court — naming conventions differ from other states

**Key Advantages for Judgemind:**
- Relatively centralized state portals reduce per-county scraping
- NYSCEF guest search allows anonymous data collection
- eCourts covers all 62 counties for civil Supreme Court
- Growing mandatory e-filing means increasing document availability

**Priority:** High. Fourth largest state by population, massive commercial litigation volume (especially NYC).

---

### 5. Illinois — Hybrid with Statewide and Local Systems

**Court System Structure:**
- 102 counties
- Circuit Courts (24 judicial circuits) — general trial courts
- Cook County (Chicago) is by far the largest and operates somewhat independently
- Appellate Courts (5 districts)
- Supreme Court

**What's Publicly Available:**
- **re:SearchIL** — Tyler Technologies statewide portal
  - researchil.tylerhost.net
  - E-filed documents across participating counties
  - Similar to re:SearchTX but coverage may be less complete
- **Cook County** — Separate systems:
  - Tyler Odyssey Portal for criminal cases (public access via Digital Access Terminals, also online)
  - Cook County Clerk of Court online docket search
  - Civil, Law, Chancery, Domestic Relations, Probate, Traffic divisions
  - Docket information only (summaries, not full documents for remote access)
  - **Critical limitation:** As of 2004, Illinois Supreme Court Electronic Access Policy "prohibits remote access to actual case documents" for circuit courts — only docket summaries available online
- **Judici** — Private service covering 82 smaller Illinois counties
  - judici.com
  - Free public case search per county
  - Courtlook (paid) for cross-county attorney/judge search
  - Background check services
- **eFileIL** — Statewide e-filing through Tyler Odyssey

**Data Access Architecture:**
- **Three-tier system:** Cook County (own portals), re:SearchIL (Tyler), Judici (82 smaller counties)
- Cook County is the single largest target and has the most restrictive remote access
- Illinois Supreme Court policy limiting remote document access is a major constraint
- Judici covers many counties but is a private service — scraping terms may be restrictive
- re:SearchIL provides e-filed documents for participating counties

**Judge Analytics Path:**
- Severely constrained by Illinois Supreme Court's electronic access policy
- Docket entries (summaries only, not documents) are the primary remote data source for Cook County
- E-filed documents via re:SearchIL may provide orders/rulings for non-Cook counties
- Judici may provide docket information for smaller counties
- In-person access or special arrangements may be needed for full document access

**Key Challenge:**
- Illinois's restrictive electronic access policy is a significant barrier
- Cook County (largest single jurisdiction) only provides docket summaries remotely
- May require advocacy/policy engagement alongside technical scraping

**Priority:** Medium. Fifth largest state but most restrictive access policies.

---

## Cross-State Analysis

### Data Type Availability Matrix

| Data Type | California | Texas | Florida | New York | Illinois |
|-----------|-----------|-------|---------|----------|----------|
| **Tentative Rulings** | ✅ Yes (unique) | ❌ No | ❌ No | ❌ No | ❌ No |
| **Dockets** | Per-county | Statewide (re:SearchTX) | Per-county (67) | Statewide (eCourts) | Split (Cook/re:SearchIL/Judici) |
| **Filed Documents** | Per-county | re:SearchTX (2016+) | Per-county (varies) | NYSCEF (e-filed) | Limited remote access |
| **Court Orders** | In dockets/tentatives | In filed documents | In filed documents | In NYSCEF/dockets | Restricted remote |
| **Judge Info** | Court websites | Court websites | Court websites | UCS website | Court websites |
| **Attorney Info** | State Bar | State Bar | Florida Bar | UCS attorney search | ARDC |
| **Statewide Portal** | ❌ (re:SearchCA limited) | ✅ re:SearchTX | ❌ | ✅ (NYSCEF + eCourts) | Partial (re:SearchIL) |
| **E-Filing System** | Various | eFileTexas | FL Courts E-Filing Portal | NYSCEF | eFileIL |

### Scraping Complexity Ranking

| State | Counties | Portals to Scrape | Template Reuse | Overall Complexity |
|-------|----------|-------------------|----------------|-------------------|
| **Texas** | 254 | 1 primary (re:SearchTX) + large county portals | High | Low-Medium |
| **New York** | 62 | 2-3 primary (NYSCEF, eCourts, WebCivil) | High | Low-Medium |
| **California** | 58 | 58 individual (tentatives) + county dockets | Moderate (5 templates) | High |
| **Illinois** | 102 | 3 systems (Cook, re:SearchIL, Judici) | Moderate | Medium |
| **Florida** | 67 | 67 individual county clerks | Low-Moderate | Very High |

### Judge Analytics Data Sources by State

| State | Primary Analytics Source | Signal Quality | Volume | Time to Useful Analytics |
|-------|------------------------|----------------|--------|-------------------------|
| **California** | Tentative rulings | **Very High** — explicit grant/deny with reasoning | Moderate (~25K/yr) | 6-12 months |
| **Texas** | Docket entries + filed orders | **Moderate** — requires NLP extraction | Very High | 12-18 months |
| **New York** | NYSCEF documents + docket entries | **Moderate-High** — full documents available | High | 12-18 months |
| **Florida** | County clerk documents + docket entries | **Low-Moderate** — fragmented, inconsistent | High | 18-24 months |
| **Illinois** | Docket summaries (limited) | **Low** — no remote document access in Cook County | Medium | 24+ months |

---

## National Court Data Ecosystem

### Existing Data Aggregators

| Service | Cases | Coverage | Price | Data Model | Relevance to Judgemind |
|---------|-------|----------|-------|------------|----------------------|
| **Trellis.law** | Not disclosed | 46 states, 3K+ courts, 2,500+ counties | $70-$200/mo | Dockets, documents, rulings, judge analytics | Primary competitor. 10-year head start on data. |
| **judyrecords** | 760M+ | Nationwide (state + federal) | Free | Case index only — no documents, no analytics | Massive case index but thin data per case. Aggregates from Tyler Odyssey portals, county sites, etc. |
| **UniCourt** | 114M+ state, 9M+ federal | 40+ states, 4,000+ courts | Freemium + API pricing | Dockets, documents, normalized entities, analytics | Closest competitor model. Strong entity normalization. API-focused. |
| **CourtListener** | 10M+ opinions | Federal (comprehensive), state appellate | Free (open source) | Opinions, oral arguments, judge database, RECAP | Complementary — federal focus. Integration target per product spec. |
| **RECAP Archive** | Hundreds of millions of entries | Federal (PACER) | Free (open source) | Federal dockets and documents | Integration target for federal court baseline. |
| **Caselaw Access Project** | 6.6M+ | All official published US case law through 2020 | Free | Published case law only | Historical case law, not trial court activity. |

### Tyler Technologies re:Search Platform

Tyler's re:Search is now live in **7 states** as a statewide court records search portal:

| State | URL | Notes |
|-------|-----|-------|
| **California** | researchca.tylerhost.net | E-filed documents; does NOT include tentative rulings |
| **Georgia** | researchga.tylerhost.net | 15+ counties live (including Fulton, DeKalb, Gwinnett) |
| **Illinois** | researchil.tylerhost.net | E-filed documents |
| **Louisiana** | researchla.tylerhost.net | — |
| **Maine** | researchmaine.tylerhost.net | — |
| **New Mexico** | researchnm.tylerhost.net | — |
| **Ohio** | researchoh.tylerhost.net | — |
| **Texas** | research.txcourts.gov | Most mature — all 254 counties |

**Strategic implications:** Tyler's re:Search creates a uniform interface across states. If we can build a scraper template for one re:Search instance, it likely works (with parameterization) for all 7 states. However, their TOS generally prohibit bulk redistribution.

### judyrecords — Deep Dive

judyrecords deserves special attention because it has scraped 760M+ cases and operates freely — and had a notable run-in with Tyler Technologies over Odyssey portal data:

- **Scale:** 760M+ cases, 100x more than Google Scholar, 10x more than PACER
- **Method:** Appears to scrape Tyler Odyssey public access portals and other county court websites
- **Tyler incident:** Tyler Technologies identified a "security vulnerability" in Odyssey portals that allowed judyrecords to access records that were supposed to be non-public. Over 1.3M case records were subsequently removed. This suggests Tyler actively monitors and may resist bulk scraping of their portals.
- **Lesson for Judgemind:** Scraping Tyler Odyssey portals at scale carries legal and relationship risk. Partnership or official data access is preferable.

---

## Strategic Implications for Judgemind Architecture

### 1. Two Data Models, Not One

The product spec treats court data as a single problem. In reality, Judgemind needs two distinct data ingestion and analytics pipelines:

**Model A: Tentative Ruling Pipeline (California only)**
- Dedicated scraper templates for tentative ruling pages
- Structured extraction: case number, motion type, outcome, judge, department, reasoning text
- Direct analytics: grant/deny rates computed from structured data
- High signal quality, moderate volume
- Ephemeral data — capture urgency is critical

**Model B: Docket/Document Analytics Pipeline (All other states)**
- Scrape docket entries and/or filed documents
- NLP extraction needed to identify: motion filings, hearing outcomes, court orders
- Classify orders as grant/deny/partial using AI
- Lower signal quality per record, higher volume needed for statistical significance
- Data is generally persistent (not ephemeral) — lower capture urgency

The architecture spec's NLP pipeline (Section 5.2) already describes much of Model B, but the product spec's emphasis on tentative ruling capture as the "single most critical system" only applies to California.

### 2. Revised State Priority Order

Based on data accessibility and value, the recommended implementation order shifts from the product spec:

| Priority | State | Rationale |
|----------|-------|-----------|
| **P1** | California | Architectural knowledge, tentative ruling uniqueness, immediate analytics value |
| **P2** | Texas | Centralized re:SearchTX, massive volume, single scraper target |
| **P3** | New York | Semi-centralized NYSCEF/eCourts, high commercial litigation value |
| **P4** | Florida | High value but extreme fragmentation — 67 county targets |
| **P5** | Illinois | Restrictive remote access policy severely limits value |

**New state candidates for P2-P3:**
- **Georgia** — re:SearchGA provides centralized access, growing coverage
- **Ohio** — re:SearchOH available
- **Louisiana, Maine, New Mexico** — re:Search portals available (lower litigation volume)

### 3. Tyler Technologies: Partner or Obstacle?

Tyler Technologies dominates the state court technology landscape:
- re:Search portal in 7 states
- Tyler Odyssey CMS in thousands of courts nationwide
- eFile platforms handling mandatory e-filing in multiple states

**Partnership opportunity:** Tyler could be an official data source — their platforms already aggregate exactly what Judgemind needs. An API partnership or data licensing arrangement could bypass scraping entirely for re:Search states.

**Risk:** Tyler's TOS prohibit bulk redistribution. judyrecords' experience shows Tyler will take action against unauthorized bulk access. Operating in this space without Tyler's awareness/consent carries risk.

**Recommendation:** Approach Tyler early. Judgemind's open-source, free, public-interest mission may resonate differently than a commercial scraping operation. Offer to credit Tyler as a data source. Explore whether their public access portals can be officially consumed.

### 4. Revised Feature Gating

The product spec's "data-gated vs. implementation-gated" distinction needs state-level granularity:

| Feature | California | Texas/NY | Florida/IL |
|---------|-----------|----------|------------|
| Docket search | Months 1-3 | Months 1-3 | Months 3-6 |
| Document search | Months 2-4 | Months 2-4 | Months 4-8 |
| Judge profiles | Months 2-4 | Months 2-4 | Months 2-4 |
| Basic alerts | Months 1-3 | Months 1-3 | Months 3-6 |
| Judge analytics | Months 6-12 (tentatives) | Months 12-18 (docket-derived) | Months 18-24+ |
| AI motion drafting | Months 6-9 | Months 9-12 | Months 12-18 |

### 5. NLP Pipeline Requirements for Non-CA States

For states without tentative rulings, the NLP pipeline must extract judge behavior from docket entries and orders. This requires:

**Docket entry classification:**
- Identify entries that represent judicial decisions (not just procedural events)
- Extract: motion type, outcome, judge, date
- Handle inconsistent terminology across states and courts
- Examples of docket text that encodes outcomes:
  - "MOTION TO DISMISS - GRANTED" (clear)
  - "Order on Defendant's Motion for Summary Judgment" (requires document analysis)
  - "Hearing held. Ruling from the bench." (outcome not in docket text)

**Order/ruling document analysis:**
- Parse court orders to extract: what was decided, for whom, on what motion
- Handle diverse formats: typed orders, handwritten notations, form orders
- Some orders reference multiple motions — must disaggregate
- Citation extraction for legal reasoning analysis

**Entity resolution across states:**
- Judge naming conventions differ by state (Justice vs. Judge vs. Hon.)
- Attorney bar number formats differ
- Case number formats are state-specific
- Party name normalization must handle state-specific entity types

---

## Open Questions

1. **Tyler Technologies partnership feasibility:** Has anyone on the team, or anyone reachable, had direct contact with Tyler's data licensing or partnership team?

2. **re:Search TOS enforcement:** How aggressively does Tyler enforce its bulk access restrictions? The judyrecords incident suggests active monitoring. Should we consult legal counsel before any re:Search scraping?

3. **Illinois remote access policy:** Is the 2004 Supreme Court Electronic Access Policy still in effect as originally written? Has there been any loosening, especially post-COVID? This policy is the single biggest barrier for IL coverage.

4. **Florida CMS landscape:** Which FL counties use which CMS? A CMS-level survey (similar to our Tyler Odyssey survey for CA) could reveal template-reuse opportunities across the 67 counties.

5. **CourtListener state court expansion:** Free Law Project has discussed expanding CourtListener to state trial courts. Are they actively pursuing this? A partnership or integration could provide significant mutual benefit.

6. **judyrecords data:** judyrecords has 760M+ case records and operates freely. Could we integrate their case index as a baseline, supplementing with our own document/ruling capture? Their data model (case index without documents or analytics) is complementary to ours.

7. **UniCourt as data source:** UniCourt has 114M+ state cases with entity normalization. Their API pricing and terms should be investigated — could they serve as a data accelerator for initial coverage?

---

## Recommendations

### Immediate (Pre-Development)

1. **Update product spec** to reflect that tentative rulings are CA-only and that judge analytics in other states require a fundamentally different data pipeline
2. **Investigate Tyler Technologies partnership** — this is the highest-leverage decision affecting multi-state strategy
3. **Consult legal counsel** on TOS implications for re:SearchTX/re:SearchIL scraping
4. **Contact Free Law Project** about CourtListener integration and potential collaboration

### Phase 1 Development (Months 1-4)

1. **California tentative ruling capture** — build the 5 scraper templates (per CA investigation)
2. **Texas docket capture** — explore re:SearchTX data access; if TOS-blocked, build Harris/Dallas/Tarrant/Travis county scrapers
3. **Docket-based analytics pipeline** — build the NLP pipeline for extracting judge behavior from docket entries and orders (needed for all non-CA states)
4. **CourtListener/RECAP integration** — immediate federal court baseline

### Phase 2 Expansion (Months 3-8)

5. **New York NYSCEF/eCourts scrapers** — semi-centralized, relatively clean data
6. **Florida** — begin with largest counties (Miami-Dade, Broward, Palm Beach, Orange, Hillsborough) and expand
7. **Georgia, Ohio** — leverage re:Search portals if Tyler partnership proceeds
8. **Illinois** — Cook County docket summaries; advocate for policy reform

---

*End of Document*
