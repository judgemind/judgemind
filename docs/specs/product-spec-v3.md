JUDGEMIND

Free, Open-Source Legal Research & Litigation Intelligence

Product Specification v3.0

March 2026

“Public court data should be publicly searchable.”

Competitive baseline: Trellis.law

AI-implemented • Human-reviewed • Open source

Changes from v2.0

Restored detailed capability/timeline planning table lost during v1→v2 transition. Restored

full descriptions of differentiated features (Section 5). Restored detailed Trellis feature

baseline tables for Collaboration, Platform, and API (Section 4.6–4.7). Restored detailed

user descriptions (Section 7). Restored inline architecture overview detail (Section 6).

Restored detailed acceleration strategy descriptions (Section 2.8). All v2.0 additions (multi-

state findings, competitive landscape, Tyler ecosystem, dual pipeline model, state-specific

roadmap) retained in full.

Table of Contents

1. What Judgemind Is

2. The Data Landscape

3. Competitive Landscape

4. Trellis.law Feature Baseline

5. Beyond Trellis: Differentiated Capabilities

6. Architecture Overview

7. Users

8. Roadmap

9. What We Will Not Do

10. Human Review: The Long Poles

11. Hosting & Cost Considerations

12. Success Metrics

13. Risks

14. Appendices

1. What Judgemind Is

Judgemind is a free, open-source legal research and litigation intelligence platform. Its goal

is to provide the same caliber of state trial court data, judge analytics, and AI-powered

litigation tools that commercial platforms like Trellis.law charge $70–$200+/month for — and

make it accessible to every attorney, public defender, legal aid organization, law student,

and self-represented litigant.

The codebase is open source. Hosting is self-funded. Implementation is AI-driven: AI agents

build the features, write the scrapers, and process the data. But building a trustworthy legal

tool also requires human review — by practicing attorneys who validate AI outputs, and

ideally by judges willing to confirm that analytics reflect their actual tendencies. That human

review is the long pole in the schedule, and this spec is honest about where it creates

bottlenecks.

How This Gets Built

There are two categories of work in this project, and they move at very different speeds:

Fast (AI-driven): Feature development, UI, search infrastructure, API design, scraper

development, document processing pipelines, NLP/ML model training. AI agents can build

and iterate on these quickly. A functional search interface, alert system, or document viewer

can go from spec to working code in days.

Slow (human-dependent): Data accumulation, legal accuracy review, judge analytics

validation, attorney vetting of AI-generated analysis, court partnership negotiations, and

building trust with the legal community. These cannot be rushed and some take months or

years. This spec does not pretend otherwise.

2. The Data Landscape

This section was substantially rewritten in v2.0 based on detailed investigation of court data

systems across all five priority states (California, Texas, Florida, New York, Illinois). The most

significant finding is that tentative rulings — the data type described in v1.0 as Trellis’s

primary moat — are a California-only phenomenon. This fundamentally changes the multi-

state strategy.

2.1 Critical Finding: Tentative Rulings Are California-Only

Trellis.law’s own support documentation explicitly confirms that tentative rulings are

available only in California. No other state in the U.S. uses this procedural mechanism in a

comparable way. This has three major implications:

1. The “moat” is narrower than assumed. Trellis’s tentative ruling archive — the 10-year

head start described in v1.0 — applies only to California. For the other 49 states, Trellis

builds judge analytics from the same docket entries, filed orders, and court documents that

are available to everyone.

2. Judgemind needs two distinct data pipelines. The architecture must support both a

tentative ruling capture pipeline (California-specific, ephemeral data, high capture urgency)

and a docket/document analytics pipeline (all states, persistent data, NLP-dependent).

These have fundamentally different engineering requirements.

3. Non-CA states may be more competitively approachable than v1.0 assumed.

Because there is no ephemeral data advantage outside California, Judgemind can build

competitive judge analytics in other states purely through docket and document analysis —

without needing to overcome a decade-long data capture head start.

2.2 Two Data Models

Model A: Tentative Ruling Pipeline (California Only)

Tentative rulings are issued by California judges 1–2 days before hearings and typically

taken down within one to two weeks. They contain explicit grant/deny decisions on specific

motions, making them the highest-signal data source for judge analytics. This pipeline

requires:

High-frequency polling of court websites (daily to hourly depending on court volume)

Immediate archival to immutable storage — missed rulings are permanently lost

Structured extraction: case number, motion type, outcome (granted/denied/partial),

judge, reasoning

Version tracking via content hashing to distinguish substantive revisions from typo

corrections

Dedicated monitoring and failure alerting — scraper downtime means data loss

Judgemind’s founding team built the original tentative ruling scraping infrastructure in 2016.

We know the publication patterns, the edge cases, and the court-by-court variations. The

capture infrastructure can be rebuilt in weeks.

Model B: Docket/Document Analytics Pipeline (All States)

For states without tentative rulings, judge analytics must be derived from docket entries and

filed court orders. This requires:

Docket entry classification: identifying entries that represent judicial decisions

NLP extraction of motion type, outcome, judge, and date from unstructured docket text

Court order/ruling document analysis: parsing orders to determine what was decided,

for whom, on what motion

Handling diverse formats: typed orders, form orders, and inconsistent clerk terminology

Higher volume requirements — docket entries are noisier and lower-signal per record

than tentative rulings

The architecture spec’s NLP pipeline (Section 5.2) already describes this model. The key

difference from v1.0 is that this is not a secondary pipeline — it is the primary analytics

pipeline for 49 states.

2.3 Why Tentative Rulings Are the Hard Part (California)

In California, judges issue tentative rulings before hearings. These are the single most

valuable data source for understanding how a judge thinks about specific motions and legal

issues. They are also ephemeral:

Tentative rulings are typically posted to court websites 1–2 days before the hearing.

They are often taken down or become inaccessible within one to two weeks.

There is no centralized archive. Each court handles publication differently.

Once they expire, they are gone unless someone captured them.

Trellis has been scraping and archiving these rulings since 2016 — initially as a one-person

operation, later as a company after converting to a C corp in 2018. That gives them roughly

ten years of historical tentative rulings across dozens of California jurisdictions. This is the

data that powers their Judge Analytics dashboard — the grant/deny rates, the ruling

tendency charts, the motion-specific analysis that attorneys actually pay for.

Judgemind starts at zero for tentative rulings. We can begin capturing tentatives from day

one, but we cannot backfill what was never archived. This has direct consequences for what

we can credibly offer and when.

2.4 What This Means for Our Roadmap: Capability Timeline

The following table maps each major capability to its data depth requirement and realistic

timeline. Timelines vary by state depending on data access architecture (see Sections 2.5–

2.6).

Capability
Data Depth

California

Timeline
TX/NY Timeline
FL/IL

Required

Timeline

Low — docket

data is

generally

Docket

Months 1–3.

Months 1–3

Months 3–6

available in

search &

Useful almost

(centralized

(fragmented

bulk from

case lookup

immediately.

portals).

access).

court systems

and does not

expire.

Low — filed

documents

persist on

Months 2–4.

Document

court systems

Grows more

search &

Months 2–4.
Months 4–8.

longer. Can

useful as the

download

begin indexing

index grows.

current filings

right away.

Low —

Basic alerts

requires only

Months 1–3.

(new

real-time

Immediately

Months 1–3.
Months 3–6.

filings, case

monitoring,

useful.

activity)

not historical

depth.

None — can be

assembled

Judge

from public

Months 2–4. AI-

biographies

sources (court

assembled,
Months 2–4.
Months 2–4.

& profiles
websites, bar

human-reviewed.

records,

news).

Low to

moderate —

Attorney /

based on

party

Months 3–6.
Months 3–6.
Months 6–9.

docket data

search

that can be

bulk-acquired.

Moderate —

useful from

Capture begins

day one for

Month 1. Search

current

Tentative

N/A — tentative

useful by Month 3.

rulings, but

ruling

rulings are CA-

N/A.

Meaningful

historical

search

only.

volume by Month

depth grows

12+.

only in real

time.

High —

Thin data for 6–12

requires

months.

18–24+

hundreds of

Potentially useful

Judge

months (IL

rulings per

for high-volume

12–18 months

Analytics

may be 24+

judge to

judges after 12–18

(docket/document

(ruling

due to

produce

months. Reliable

NLP).

tendencies)

access

statistically

at Trellis-level

restrictions).

meaningful

depth: 3–5+

patterns.

years.

Moderate —

depends more

AI tooling

on model

buildable in

AI Case

quality than

Months 3–6.

Months 3–6.
Months 3–6.

Assessment

data depth,

Quality improves

but benefits

as data grows.

from local

precedent.

High — needs

Basic capability

a deep corpus

Months 6–9.

of similar

Improves

AI Motion

motions and

significantly as
Months 9–12.
Months 12–

Drafting
their outcomes

corpus grows. Will

18.

to generate

not match Trellis

credible

quality for 12–18+

drafts.

months.

Do not promise

Very high —

until 18–24+

requires years

Predictive

months of data at

of outcome

Outcome

minimum.

24+ months.
24+ months.

data to train

Modeling

Premature claims

meaningful

would damage

models.

credibility.

2.5 California: The Tentative Ruling Landscape

California has 58 counties with no statewide case management system. Each county

publishes tentative rulings independently. Investigation identified six distinct publication

patterns:

Pattern
Counties
Scraping Approach

LA Superior, Orange,

Scrape listing page, follow links to

Dedicated tentative

Riverside, San Bernardino,

individual rulings. Most

rulings page

others

straightforward.

Tyler Odyssey portal

28+ counties using Tyler

One scraper template parameterized

with tentative

CMS

per county.

search

PDF posting (daily

tentatives as PDF)
Several mid-size counties
Download PDF, OCR/extract text,

parse into structured records.

Court-specific

San Francisco, Santa Clara,

others
Per-court custom scraper required.

custom portal

No online

publication
Many small/rural counties
Cannot be scraped. Requires court

partnership.

Third-party

Some counties only via

May require partnership or alternative

aggregation only

tentativerulings.org

acquisition.

The top four counties by population (Los Angeles, Orange, Riverside, San Bernardino) can

be covered with approximately two scraper templates and account for roughly 60% of

California’s civil litigation volume. Statewide, approximately 25,000 tentative rulings are

issued per year.

California Competitors for Tentative Rulings

tentativerulings.org: Covers approximately 25 California counties. Paid subscription.

Potential partnership target.

rulings.law: Free service covering approximately 180 California judges. Demonstrates

viability.

Bench Reporter: LA County focused. $39/month.

Trellis.law: ~10 years of historical tentative ruling data powering California judge

analytics.

2.6 State-by-State Data Access

Investigation of the five priority states revealed dramatically different data access

architectures.

Texas

Architecture: Centralized. Texas has re:SearchTX (research.txcourts.gov), a Tyler

Technologies statewide portal covering all 254 counties. Civil, family, and probate cases.

Documents from 2016+ (attorneys) / 2018+ (public). One scraper template could cover the

entire state. Risk: TOS prohibit “bulk copying” and creating searchable databases. Tyler

actively enforces (see judyrecords incident, Section 3.2). Large counties also have

independent portals. No tentative rulings.

New York

Architecture: Semi-centralized. Two primary statewide portals: NYSCEF (electronic filing,

Supreme Court civil, Court of Claims, guest search available) and eCourts (docket portal, all

62 counties from 1998+). Scraping complexity: low-medium with 2–3 primary targets and

high template reuse. No tentative rulings. Third-party CourtAlert offers NYSCEF monitoring

at $1.50/day/case.

Florida

Architecture: Fully fragmented. No statewide case search portal. Each of 67 counties has

its own system. Mix of Tyler Odyssey, Civitek (ClericusCMS, Florida-specific), and other

vendors. E-Filing Portal exists for filing only, not search. CCIS (statewide aggregation)

restricted to government agencies. Access governed by FL Supreme Court AOSC24-65 with

security matrix. Some counties charge per-page fees. Scraping complexity: very high — 67

separate targets with low template reuse. No tentative rulings.

Illinois

Architecture: Three-tier with access restrictions. re:SearchIL (Tyler, limited adoption),

Cook County (separate Odyssey portal, docket summaries only — no full documents

remotely), and Judici (private service, 82 smaller counties). Critical barrier: 2004 IL

Supreme Court policy prohibits remote access to circuit court documents. Post-COVID

status unclear. Most restrictive access of the five states. No tentative rulings.

2.7 Data Type Availability Matrix

Data Type
California
Texas
New York
Florida
Illinois

Tentative

Rulings
YES (unique)
No
No
No
No

Dockets
Per-county (58)
Statewide

Statewide

Per-county

Split (3

portal

portal

(67)

systems)

Very

Filed

Documents
Per-county
re:SearchTX

(2016+)
NYSCEF
Per-county

limited

remotely

Court

In

In

In

In

documents
Restricted

Orders

dockets/tentatives

documents

NYSCEF

Statewide

Portal
No
Yes
Yes
No
Partial

2.8 Scraping Complexity Ranking

Rank
State
Complexity
Primary Targets
Template Reuse

1 (easiest)
Texas
Low-Medium
1 primary portal
Very high

2
New York
Low-Medium
2–3 portals
High

3
California
High
58 county targets
Moderate

4
Illinois
Medium
3 systems
Moderate

5 (hardest)
Florida
Very High
67 county targets
Low

2.9 Judge Analytics: Signal Quality by State

State
Primary Signal Source
Signal

Time to Useful

Quality

Analytics

California
Tentative rulings (explicit

grant/deny)
Very High
6–12 months

Texas
Docket entries + filed orders
Moderate
12–18 months

New York
NYSCEF documents + dockets
Moderate-

High
12–18 months

Florida
County documents + dockets
Low-

Moderate
18–24 months

Illinois
Docket summaries only
Low
24+ months

2.10 Strategies to Accelerate Data Accumulation

We are not limited to passively scraping and waiting. Several strategies could compress the

timeline, though none are guaranteed:

Tyler Technologies partnership (highest leverage). Tyler operates re:Search portals in at

least 7 states and Odyssey CMS in thousands of courts. A data licensing or API partnership

could bypass scraping entirely for re:Search states. One partnership could be worth more

than dozens of individual court relationships. The open-source, free, public-interest framing

may resonate differently than a commercial scraping operation. This is the single highest-

leverage strategic decision affecting multi-state strategy.

Direct court partnerships. Approach courts and judges directly and offer to host their

tentative rulings in a permanent, searchable, public archive. Many courts struggle with their

own technology and may welcome a free, well-built solution. If a court agrees to push

rulings to our API (or let us pull from theirs), we get structured data without scraping and

potentially access to historical archives they maintain internally. This is high-leverage but

requires relationship-building with court administrators, which is slow and human-

dependent.

Bulk historical requests. Some courts or court systems may have internal archives of past

tentative rulings that are technically public record but not published online. Public records

requests (or equivalent mechanisms) could unlock years of historical data in one batch.

Success will vary by jurisdiction and court.

Community contribution. Because Judgemind is open source, law firms, law schools, legal

aid organizations, and individual practitioners could contribute historical data they have

saved. A firm that has been tracking a particular judge’s rulings for years could upload that

data. This is unpredictable but could provide unexpected depth in specific courts.

Law school partnerships. Law school clinics and libraries are natural allies. They have

institutional interest in accessible legal data, often have existing collections, and can

provide both data and human review capacity (students supervised by professors). Harvard,

Stanford, Georgetown, and others already study judicial analytics.

Integration with existing open data. CourtListener (Free Law Project) already archives

federal opinions and some state appellate data. RECAP archives federal court documents

from PACER. Building on and integrating with these existing open-source efforts avoids

duplicating work and may provide significant data depth in federal courts from day one.

CA tentative ruling services as partnership targets. tentativerulings.org (25 counties,

~25,000 rulings/year) and rulings.law (~180 judges) are potential partnership or data-

sharing targets that could accelerate California-specific historical depth.

Honest assessment: Even with all accelerators, judge analytics for state trial courts will take

12–18 months to become meaningfully useful in California (where tentative rulings provide

the highest-signal data), and 18–24+ months in other states where analytics depend on

NLP extraction from docket entries and filed orders. We should say so publicly. Overselling

this would undermine the project’s credibility with the exact audience we need to trust us.

2.11 Architectural Knowledge Advantage

Judgemind’s founding team has direct, firsthand experience building state court scraping

infrastructure from scratch — including knowledge of which court systems publish what,

how tentative ruling publication works across jurisdictions, where the edge cases and

gotchas are, and what a production-grade ingestion pipeline actually looks like. This does

not close the data gap, but it means we are not starting from zero on the engineering side.

We know what to build because we have built it before.

This knowledge translates into concrete advantages: we know which courts are scrapeable

and which require alternative approaches, we know the failure modes and how to handle

them, and we know how to structure the data for the analytics that matter. The ten-year

head start on data is real, but we can build the capture infrastructure in weeks rather than

the months it took the first time around.

2.12 Three Threshold Questions Before Non-CA Development

1. Can we partner with Tyler Technologies? One partnership could provide structured

data for 7+ states.

2. What are the legal risks of scraping re:Search portals? Legal counsel must review

TOS before any re:Search scraping.

3. Is Illinois’s 2004 access restriction still in effect? If loosened post-COVID, IL becomes

more tractable.

3. Competitive Landscape

New in v2.0. Investigation revealed a richer competitive landscape than initially assessed.

3.1 Direct Competitors

Trellis.law

Primary competitive baseline. 46 states, 3,000+ courts, 2,500+ counties. Pricing: $70–

$200+/month. Key differentiator: ~10 years of California tentative ruling data. Their own

support docs confirm tentatives are CA-only. For non-CA states, their judge analytics use

the same docket/document data available to everyone.

judyrecords

Free court records search engine. 760M+ indexed cases — 100x more than Google Scholar,

10x more than PACER. Case index only — no documents, analytics, or judge intelligence.

Appears to scrape Tyler Odyssey public access portals and other county court websites.

The judyrecords incident: judyrecords scraped Tyler Odyssey portals and inadvertently

captured non-public records due to a vulnerability in the Odyssey architecture. Tyler

identified the issue and over 1.3 million records were removed across multiple states. The

incident demonstrates that Tyler monitors bulk access and can shut down unauthorized

data consumption.

UniCourt

114M+ state cases, 40+ states, 4,000+ courts. Freemium + API model. Strong entity

normalization. Potential reference for entity resolution approaches.

3.2 Tyler Technologies: The Ecosystem

Tyler Technologies is the dominant court technology vendor in the U.S. Any state court data

strategy must account for them.

re:Search Platform: Statewide portals in 7+ states (TX, CA, GA, IL, LA, ME, NM, OH).

Uniform interface — one scraper template could work for all.

Odyssey CMS: Deployed in thousands of courts. Counties without re:Search often have

individual Odyssey portals.

E-Filing Platforms: Tyler operates e-filing in multiple states, controlling the data

pipeline.

TOS enforcement: re:Search prohibits bulk copying and searchable database creation.

Actively enforced (judyrecords incident).

Strategic Question: Partner or Obstacle?

Partnership opportunity: Tyler aggregates what Judgemind needs. API partnership could

bypass scraping for re:Search states. Judgemind’s open-source, free, public-interest

mission may resonate differently than a commercial scraping operation.

Risk: Free open-source redistribution of Tyler portal data may be viewed as threatening.

Recommendation: Approach Tyler early, before scraping begins. Lead with public-interest

mission. If unwilling, fall back to respectful human-speed scraping of individual court

portals.

3.3 Open-Source & Public-Interest Data Sources

CourtListener (Free Law Project): 10M+ federal/state appellate opinions. RECAP

Archive. Integration target for federal baseline.

RECAP: Browser extension + archive for federal PACER documents. Hundreds of

millions of entries.

Caselaw Access Project: 6.6M+ published US case law through 2020. Historical case

law, not trial court activity.

tentativerulings.org: ~25 CA counties. Paid. Potential partnership.

rulings.law: ~180 CA judges. Free. Demonstrates viability.

4. Trellis.law Feature Baseline

Trellis.law began as a scraping operation in 2016, converted to a C corp in 2018 inheriting all

accumulated data, and has since grown into the largest US state trial court database

serving tens of thousands of law firms. The following is a complete inventory of their known

features as of March 2026, compiled from their public website, support documentation,

press coverage, API docs, and review sites. Every feature here is in scope for Judgemind.

The “Judgemind Status” column indicates whether the feature is data-dependent (gated by

accumulation timelines) or implementation-dependent (can be built as soon as engineering

reaches it).

v2.0 clarification: “DATA-GATED” for tentative ruling features applies to California only. For

all other states, equivalent analytics use the Model B (docket/document NLP) pipeline with

different timelines per Section 2.4.

4.1 Data & Coverage

Trellis

Feature
Details
Judgemind Status

Implementation-gated. Build

State trial

46 states, 3,000+ courts, 2,500+

scrapers state by state. Start with

court

counties. Dockets, documents,

CA, TX, NY (best data access). FL

database

rulings, motions.

and IL follow. Add states

continuously.

Can integrate with

Federal

Federal district, appellate, Supreme

CourtListener/RECAP on day one for

court

Court, bankruptcy. PACER

significant federal coverage at zero

coverage

integration.

cost.

Implementation-gated. Scraper

Real-time

Continuously updated with latest

infrastructure is straightforward AI

ingestion

filings. Daily filing reports.

work.

~10 years of captured tentative

Tentative

rulings (collection began 2016)

DATA-GATED (CA only). This is the

rulings

across dozens of jurisdictions.

moat. We start at zero. See Section

archive

CALIFORNIA ONLY (confirmed by

2.

Trellis docs).

Grows over time. Will be thin

Document

Hundreds of millions of motions,

initially. Federal documents via

repository

briefs, pleadings.

RECAP help.

Mixed. Some verdict data is in

Verdict &

Searchable verdicts, arbitration

dockets. Dedicated verdict

settlement

awards, settlements.

databases may require

data

partnerships.

State rules
Implementation-gated. Rules are

library
State-specific procedural rules.
public and can be compiled quickly.

4.2 Search & Discovery

Trellis

Feature
Details
Judgemind Status

Smart

Implementation-gated. Full-text

Proprietary search with Boolean

Search

search + semantic search buildable

operators across all data types.

(NLP)

early.

Filter by case type, jurisdiction,

Advanced

Implementation-gated. Standard

judge, date, motion type, party,

filters

faceted search.

attorney.

Enhanced, searchable dockets with

Docket

Implementation-gated. Among the

added context vs. raw county

search

first features to ship.

dockets.

Ruling

Full-text search across rulings

Data-gated for depth. Immediately

search

including tentatives.

useful for new rulings as captured.

Document

Full-text search across filed

Grows with corpus. Useful early for

search

documents.

new filings.

Motions &

Organized directory of motion types

Implementation-gated. Can be AI-

Issues

with playbooks and strategies.

generated and attorney-reviewed.

directory

4.3 Analytics & Intelligence

Trellis Feature
Details
Judgemind Status

Ruling

tendencies,

DATA-GATED. The flagship feature. CA: tentative

grant/deny

ruling data. Others: docket/document NLP (Model

Judge Analytics

rates, motion-

B). Ship the UI early, but label analytics as

dashboard

specific

provisional until volume thresholds are met per

analysis,

judge. Show exactly how many rulings each

comparative

metric is based on.

analysis.

Background,

education,

Judge biographies
career,

Implementation-gated. AI can compile from public

notable

sources. Attorneys/judges review for accuracy.

rulings.

Opposing

counsel case

Partially data-gated. Docket-based metrics

history,

Attorney analytics

available early. Win-rate analysis needs ruling

success rates,

depth.

docket

activity.

Firm-level

litigation

Law firm

Same as attorney analytics. Aggregation layer on

patterns, case

intelligence

top.

types,

performance.

Litigation

exposure for

Corporate/company

Partially data-gated. Case counts available early.

companies: all

reports

Trend analysis needs 6–12 months of data.

cases, trends,

risk.

AI-generated

case

Implementation-gated. Buildable as soon as we

Case summaries

overviews

can retrieve case documents.

with key facts,

claims, status.

4.4 Alerts & Monitoring

Trellis Feature
Details
Judgemind Status

Notifications on new

Case docket

Implementation-gated. One of the easiest

filings/activity for

alerts

features to build early.

tracked cases.

Notifications when a

judge issues new

Judge alerts

Implementation-gated.

rulings or gets new

cases.

Alerts on new

Implementation-gated. CA tentatives + docket-

tentative rulings

based for other states. Useful from day one —

Ruling alerts

matching keywords.
alerts fire on new rulings as captured.

Track when a party or

Party/attorney

attorney appears on

Implementation-gated.

alerts

new filings.

Alert

Multiple recipients per

alert, self-exclusion.
Implementation-gated.

recipients

Automated daily

Daily filing

digest of new filings

Implementation-gated.

reports

by jurisdiction/case

type.

4.5 AI Tools (Trellis AI, launched Nov 2024)

Trellis launched its AI suite in November 2024. Several tools are jurisdiction-limited (Draft

Arguments is CA-only at launch). Judgemind will build equivalents for all of these, but

quality of output is directly tied to data depth.

Trellis AI Tool
What It Does
Judgemind Approach

AI-generated motion

Build the tool early. Quality will start

drafts (MSJ, MTD). Draws

lower than Trellis due to thinner corpus.

Draft Arguments

from similar cases.

Improve as data grows. Be transparent

Includes citations. CA-

about corpus size backing each draft.

only expanding.

Comprehensive analysis:

Implementation-gated. Can produce

key facts, claims,

Case Assessment

useful output even with thin local data

defenses, outcomes, next

by leveraging general legal knowledge.

steps.

Analyzes complaint,

Defense

Implementation-gated. Similar to Case

generates defense

Recommendations

Assessment.

strategies.

Implementation-gated. Document

Argument

Dissects arguments and

analysis does not require deep

Analyzer

evidence in a document.

historical data.

Identifies parties, roles,

Implementation-gated. NLP/entity

Party Identifier

and involvement from

extraction task.

documents.

Timeline

Chronological event

Implementation-gated. Straightforward

Generator

timeline from documents.

extraction task.

Document

Concise summaries of

Implementation-gated. Among the

Summarizer

dense filings.

easiest AI tools to build well.

Citation Checker
Verifies citation accuracy

Implementation-gated. Can cross-

in uploaded documents.

reference against case law databases.

Combines alerts (implementation-

Early case assessments

Case Strategy

gated) with Case Assessment

delivered via alerts when

Reports

(implementation-gated). Buildable

client is named.

early.

4.6 Collaboration & Platform

Trellis

Feature
Details
Judgemind Status

Share results and

Share with

documents with

Implementation-gated.

Envelopes

colleagues.

Document

Download documents

from dockets.
Implementation-gated.

download

Print &

Print/export analytics,

results, rulings.
Implementation-gated.

export

Implementation-gated. Our version:

User

Manage users/seats

organization accounts with role-based access

management

within a subscription.

(but free).

Docket

On-demand refresh of a

docket for latest events.
Implementation-gated.

refresh

4.7 API

Trellis

Feature
Details
Judgemind Status

Case, judge, document, metadata

Implementation-gated. Build API-

REST API

search. Download records

first; the web UI is a client of the API.

programmatically.

Implementation-gated. No purchases

Webhooks
Notifications for alerts, refreshes,

needed since we are free.

document purchases.

Implementation-gated. Can integrate

PACER

Federal court access via PACER

RECAP/CourtListener for free

API

integration.

equivalent.

Alert API
Programmatic alert management.
Implementation-gated.

4.8 Trellis Pricing (for reference)

Trellis charges the following as of March 2026. Judgemind’s answer to all tiers is: free.

Tier
Price
Key Limits

Personal
$69.95/mo

240 content views/yr. Single state. No Judge

($649.95/yr)

Analytics.

Research
$129.95/mo

($1,099.95/yr)
900 views/yr. Single state. No Judge Analytics.

Research +

$199.95/mo

900 views/yr. Single state. Adds Judge

Analytics

($1,999.95/yr)

Analytics.

Enterprise
Custom
Multi-state, API, Firm Intelligence, Company

Reports, Trellis AI.

5. Beyond Trellis: Differentiated Capabilities

The following features are things Trellis does not offer. These are in scope for Judgemind

but are not all Phase 1. They are ordered roughly by feasibility — features near the top are

less data-dependent and can ship earlier. Features near the bottom require significant data

depth and should not be promised until the data supports them.

5.1 Collaborative Case Workspaces

Data dependency: None. This is pure application logic.

Trellis’s sharing is limited to one-way “Envelopes.” Judgemind can offer team-oriented case

rooms where multiple people collaborate on a single matter with shared research,

annotations, saved searches, and task assignments. Since Judgemind is free, there is no

per-seat friction — an entire legal aid office can use it. This feature is especially valuable for

public defenders and legal aid organizations that are underserved by commercial tools.

5.2 Multi-Jurisdiction Comparator

Data dependency: Low for rules/statutes. High for judicial tendency comparisons.

Side-by-side comparison of procedural rules, motion standards, and (eventually) judicial

tendencies across jurisdictions. The statutory and rule comparison layer can ship early since

rules are public and static. The judicial analytics comparison layer requires data depth per

Section 2 and should be phased in as data allows.

5.3 Client-Facing Reports

Data dependency: None for the report generation itself. Content quality depends on

underlying data.

Exportable, white-labeled case status reports and litigation exposure dashboards. Useful for

any attorney who needs to present case intelligence to a client. Trellis does not offer this.

The template and export system is implementation-gated. The quality of the analytics in the

reports depends on data depth.

5.4 AI Deposition Preparation

Data dependency: Low. Operates primarily on user-uploaded case documents.

Upload complaint, discovery responses, and witness information. AI generates deposition

question outlines organized by topic, surfaces inconsistencies between witness statements

and documentary evidence, and produces deposition summaries and digests from

transcripts. No competitor offers dedicated deposition prep tooling. This feature operates

mostly on the user’s own documents so it does not require deep platform data.

5.5 Discovery Intelligence

Data dependency: Low to moderate. Core features work on uploaded documents.

AI tools for the discovery phase: auto-categorize document productions, generate

interrogatory and RFP drafts from complaint analysis, assist with privilege log generation,

and provide a review dashboard with AI-suggested coding decisions. Like deposition prep,

this mostly operates on the user’s own documents rather than requiring platform data

depth.

5.6 Brief Bank / Knowledge Management

Data dependency: None. User-contributed content.

Allow organizations to build an internal searchable repository of their own briefs, memos,

and research, layered on top of Judgemind’s public data. AI-powered semantic search

across both internal and public documents. Especially valuable for legal aid organizations

and public defender offices that currently have no institutional knowledge management.

5.7 Expanded AI Motion Drafting (50-state)

Data dependency: High. Draft quality directly correlates to corpus depth.

Trellis’s Draft Arguments launched CA-only and is expanding. Judgemind will support all

states from day one architecturally, but we should be transparent that draft quality will be

uneven across jurisdictions until data depth catches up. Show users the corpus size backing

their draft. A draft based on 50 similar motions is more trustworthy than one based on 3.

5.8 Predictive Outcome Modeling

Data dependency: Very high. Do not ship this prematurely.

ML-based prediction of how a judge is likely to rule on a specific motion given case

characteristics. This is the most data-hungry feature and the one most likely to damage

credibility if shipped too early. Do not promise this until we have at least 18–24 months of

ruling data and have validated model accuracy with practicing attorneys. It is better to ship

this late and accurate than early and unreliable.

6. Architecture Overview

See companion document: Judgemind Architecture Specification v1.0 for full technical

details. The summary below provides enough architectural context for the product spec to

stand alone.

API-first. The web application is a client of the API, not the other way around. Every

capability exposed in the UI is available programmatically. The public REST API and the

internal GraphQL API share the same data access layer.

v2.0 addition — Dual data pipeline. Architecture must explicitly support both Model A

(tentative ruling capture, CA-only, ephemeral data, high urgency) and Model B

(docket/document NLP analytics, all states, persistent data, NLP-dependent) as parallel

ingestion paths feeding the same downstream analytics and search infrastructure.

6.1 Data Layer

Court data ingestion: Modular scraper framework. One scraper per court system. AI

agents write and maintain scrapers. Failures alert automatically. Five scraper templates

cover the majority of California counties; centralized portals (re:SearchTX,

NYSCEF/eCourts) enable single-template coverage for TX and NY.

Tentative ruling capture (Model A): High-frequency polling (multiple times daily) for

jurisdictions that publish tentatives. Immediate archival. Dedicated monitoring. This is

the critical pipeline for California.

Docket/document analytics (Model B): NLP extraction pipeline for judge behavior

from docket entries and filed orders. The primary analytics pipeline for 49 states.

Document store: Object storage (S3-compatible) with full-text indexing via

Elasticsearch/OpenSearch. OCR pipeline for scanned filings.

Structured data: PostgreSQL for dockets, parties, judges, attorneys, case metadata.

Designed for analytical queries.

Vector store: Embeddings of all documents for semantic search and RAG. Qdrant

(dedicated vector DB, open-source, self-hostable).

External data integration: CourtListener, RECAP, and other open legal data sources

integrated as first-class data sources.

6.2 AI/ML Layer

Multi-model, three tiers: Tier 1 (ingestion NLP, <$0.01/doc, start with Haiku-class,

migrate to self-hosted at volume). Tier 2 (on-demand analysis, $0.02–$0.10/call,

commercial APIs). Tier 3 (heavy generation, $0.10–$0.50/call, commercial APIs).

RAG pipeline: All generative AI grounded in actual court filings via retrieval-augmented

generation. Every AI output should cite its sources.

NLP pipeline: Entity extraction (parties, judges, attorneys, dates, amounts), motion type

classification, outcome classification. Runs on every ingested document. For Model B

states, this pipeline must additionally classify docket entries as judicial decisions and

extract ruling outcomes from unstructured text.

Human-in-the-loop: AI outputs for analytics and legal analysis are flagged for human

review. Reviewed outputs feed back into training data. Attorney and judge reviewers are

credited (with permission).

6.3 Application Layer

Web application: Next.js (React, TypeScript). Server-side rendering for SEO. Real-time

updates via WebSocket for alerts.

GraphQL API (internal, frontend): Next.js frontend communicates exclusively with

GraphQL. Optimized for the deeply relational legal data model.

REST API (public, third-party): RESTful with OpenAPI 3.0 spec. Versioned.

Authenticated via API key. Rate-limited to prevent abuse but generous for legitimate

use.

Webhooks: Event-driven notifications for alerts and data updates.

Open source: Entire codebase on GitHub. Permissive license (Apache 2.0 or similar).

Contributions welcome. Deployment documentation for self-hosting.

6.4 Security & Trust

Encryption: TLS in transit, AES-256 at rest for any user-uploaded documents.

User data isolation: User uploads and case workspaces are private. The platform itself

works with public court data.

AI transparency: Every AI-generated output includes a disclosure that it is AI-

generated, the model used, and the data sources consulted. No black boxes.

No training on user data: User uploads and queries are never used to train models.

Audit log: All searches and AI interactions logged for the user’s own records. Not

shared.

Authentication: Email/password + OAuth (Google, Microsoft). SSO for organizations

that need it.

7. Users

Being free changes the user base dramatically compared to Trellis. Judgemind is not just for

BigLaw.

7.1 Primary Users

Solo practitioners and small firms. Currently priced out of Trellis ($840–$2,400/year).

This is the largest segment of the legal profession and the most underserved by litigation

analytics tools.

Public defenders and legal aid. Have zero budget for commercial legal research beyond

what their jurisdiction provides. Judgemind could be the first analytics tool many public

defenders have ever had access to.

Law students and clinics. Legal research training currently depends on access to

Westlaw/LexisNexis through school subscriptions. Judgemind gives students and clinics a

free state court research tool they can use after graduation.

In-house counsel (small/mid companies). Companies without the budget for enterprise

Trellis subscriptions. Need litigation monitoring and risk assessment.

7.2 Secondary Users

Mid-size and large firms. May already have Trellis. Judgemind competes on openness,

transparency, and the specific differentiators in Section 5. Some firms will use both.

Journalists and researchers. Court data is essential for investigative journalism and

academic research on judicial behavior. Free access with API makes Judgemind a research

tool.

Legal tech developers. Open API lets other tools build on Judgemind’s data. This creates

an ecosystem rather than a walled garden.

Self-represented litigants. People navigating the court system without an attorney.

Judgemind won’t replace legal advice, but it can help them understand how their judge has

ruled on similar issues.

8. Roadmap

Updated from v1.0 in v2.0 to reflect investigation findings. Major changes: state-specific

phasing based on scraping complexity, explicit Tyler Technologies dependency, Model B

NLP pipeline as Phase 1 priority, and dual pipeline architecture.

8.1 Phase 1: Infrastructure & California (Months 1–4)

Goal: Build both ingestion pipelines, start capturing California tentative rulings from day

one, and ship features useful with thin data.

California tentative ruling capture pipeline (Model A): Begin with LA, Orange, Riverside,

San Bernardino (~2 scraper templates, ~60% of state volume).

Docket/document analytics pipeline (Model B): NLP extraction of motion types,

outcomes, and judicial decisions from docket entries and filed orders. Serves all non-CA

states.

Scraper framework: modular, one-per-court-system architecture. AI agents write

scrapers. Begin with California.

Tentative ruling capture pipeline: high-frequency polling, archival, and full-text indexing.

Every ruling captured from this point forward is data we own.

Docket search and case lookup: California first. Texas (re:SearchTX) pending legal

review of TOS.

Document indexing and full-text search for newly filed documents.

Real-time alerts: case docket alerts, ruling alerts, party alerts. Useful immediately.

Judge profile pages: biographical information compiled from public sources. AI-

assembled, queued for human review.

API v1: core endpoints for case, docket, document, and judge search.

CourtListener/RECAP integration for federal court baseline data.

User accounts and basic organization management.

Open-source repository published. Contribution guidelines. Self-hosting docs.

STRATEGIC: Initiate Tyler Technologies partnership conversation.

STRATEGIC: Retain legal counsel to review re:SearchTX and Odyssey TOS.

8.2 Phase 2: AI Tools & Multi-State Expansion (Months 3–8)

Goal: Launch AI-powered document tools (which work on user uploads and don’t require

deep platform data) and expand state coverage based on Tyler strategic decision.

AI Document Summarizer, Timeline Generator, Party Identifier, Argument Analyzer.

These operate on uploaded documents and are useful immediately.

AI Case Assessment: generates case overview from uploaded complaint. Useful even

with thin local data.

Citation Checker: cross-reference citations against known case law.

Expand California tentative ruling capture: add 15–20 more counties based on litigation

volume and data accessibility.

If Tyler partnership succeeds: activate re:Search scraping for TX, then GA, OH, and

other re:Search states via single template.

If Tyler partnership fails: begin respectful human-speed scraping of TX large county

portals (Harris, Dallas, Tarrant, Travis, Bexar) and NY (NYSCEF, eCourts).

Attorney and party search with basic docket-derived metrics.

Motions & Issues directory: AI-generated, attorney-reviewed playbooks for common

motion types.

Collaborative case workspaces: shared research, annotations, task assignments.

Document export and sharing features.

8.3 Phase 3: Analytics & Differentiation (Months 6–14)

Goal: Begin surfacing analytics where data supports it. Ship differentiators. Expand

coverage.

Judge Analytics dashboard: ship the UI, but show analytics only for judges where we

have crossed minimum data thresholds. Display sample sizes prominently. Label

everything as provisional where appropriate.

California analytics first (tentative ruling data gives fastest path to useful judge profiles).

Non-CA analytics: begin surfacing Model B NLP-derived analytics for high-volume

judges in TX, NY.

Law firm and corporate litigation reports: aggregate docket data into firm-level and

company-level views.

AI Motion Drafting: initial version. Be transparent about corpus depth. Show users how

many similar motions informed the draft.

Multi-Jurisdiction Comparator: statutory and procedural rule comparisons first. Judicial

tendency comparisons only where data supports it.

Client-facing report exports with customizable branding.

Deposition preparation tools: question generation, inconsistency detection, digest

creation.

Discovery intelligence: document categorization, interrogatory/RFP drafting.

Brief bank / knowledge management for organizations.

Begin Florida scraping (5 largest counties: Miami-Dade, Broward, Palm Beach, Orange,

Hillsborough).

Illinois: if access policy loosened, begin Cook County + re:SearchIL. If not, deprioritize.

Target: 20–30 states with active data ingestion.

8.4 Phase 4: Depth & Trust (Months 12–24+)

Goal: As data accumulates, unlock features that require depth. Build credibility through

accuracy.

Judge Analytics matures: more judges cross data thresholds. CA leads, TX/NY follow.

Analytics become genuinely useful for common judges in covered jurisdictions.

AI Motion Drafting quality improves as document corpus grows. Jurisdiction-specific

quality metrics published.

Court partnership program: formalize relationships with courts willing to share data or

post rulings to our API.

Predictive outcome modeling: begin development only after 18+ months of data.

Validate with practicing attorneys before any public claims. Ship as experimental/beta

with prominent disclaimers.

Community data contributions: accept and validate contributed historical data.

Target: 40–50 states with active ingestion. Full coverage aspirational.

Federal court depth beyond CourtListener baseline.

9. What We Will Not Do

This is as important as the feature list. Judgemind’s credibility depends on honesty about

limitations.

We will not claim our judge analytics are comprehensive until data depth supports it.

Every analytic will show its sample size. If we have 12 rulings for a judge, we will say so.

We will not ship predictive outcome modeling until models have been validated by

practicing attorneys against real outcomes. Premature predictions could cause real

harm to real cases.

We will not present AI-generated legal analysis without clear disclosure. Every AI output

is labeled as AI-generated with sources cited.

We will not claim parity with Trellis’s data depth in marketing or documentation until we

have actually achieved it. Our advantage is being free and open, not having more data

(yet).

We will not lock features behind paywalls, premium tiers, or usage caps. If someone

needs to pay for legal research, they should use Trellis. Judgemind is free or it is

nothing.

We will not train models on user-uploaded documents or user queries.

We will not provide legal advice. The platform provides data, analytics, and AI-assisted

research tools. It is not a substitute for professional legal judgment.

10. Human Review: The Long Poles

The following components require human review by attorneys, judges, or legal

professionals. AI builds the first draft; humans validate, correct, and approve. This is the

work that cannot be parallelized indefinitely and will pace certain capabilities.

Component
Who Reviews
Estimated Effort
Can It Be

Crowdsourced?

Yes. Law school

Judge

Attorneys, law

15–30 min per judge.

clinics, bar

biographies

librarians

Thousands of judges.

associations, individual

attorneys.

Partially. Core

Motions

playbooks
Practicing litigators
1–2 hours per motion

structure by AI,

type per jurisdiction.

reviewed by subject-

matter attorneys.

Yes. Feedback buttons

AI tool

Ongoing. Spot-check AI

Attorneys across

on every AI output.

output

outputs against attorney

practice areas

Reviewed outputs

quality

judgment.

improve models.

Ideally judges

Varies. Comparing

Judge

themselves;

Difficult. Requires trust

analytics to a judge’s

analytics

otherwise

and relationship.

self-assessed

accuracy

experienced local

Individual outreach.

tendencies.

practitioners

Significant. Requires

Predictive

Partially. Can solicit

Litigators with

comparing predictions to

model

outcome data from

outcome data

actual outcomes across

validation

willing practitioners.

many cases.

Project leads,

Court

Months per court

No. Requires personal

potentially

partnership

system. Relationship-

credibility and

attorneys with

negotiations

dependent.

institutional trust.

court relationships

11. Hosting & Cost Considerations

Since this is self-funded and free to users, cost management matters. The architecture

should be designed to minimize hosting costs while maintaining performance.

Cloud hosting: Start on a single cloud provider (AWS, GCP, or equivalent). Use

reserved/spot instances where possible.

AI API costs: The largest variable cost. Use open-source models (Llama, Mistral)

running on self-hosted GPU instances for high-volume NLP tasks (entity extraction,

classification, embeddings). Reserve commercial API calls (Anthropic, OpenAI) for user-

facing generation tasks where quality matters most.

Storage: Court documents are large in aggregate. Use tiered storage: hot storage for

recent/popular documents, cold storage for archival. Compress aggressively.

Rate limiting: Generous for normal use. Aggressive against scraping/abuse. API keys

required for programmatic access.

Cost monitoring: Alerts at spending thresholds. If AI API costs grow faster than

expected, adjust rate limits on AI features before they become unsustainable.

Community hosting: Because it is open source, organizations can self-host the entire

platform if they want. This means our hosting costs serve the public instance, not the

only instance.

Rough estimate: A production instance serving tens of thousands of users with moderate AI

usage could run $2,000–$8,000/month depending on compute, storage, and AI API costs.

This is a meaningful personal expense but manageable for a funded individual, especially

with optimization.

12. Success Metrics

Unlike a commercial product, success is not measured by revenue. Success for Judgemind

means:

A public defender in a rural county can look up how their judge tends to rule on motions

to suppress, for free, for the first time ever.

A solo practitioner preparing for their first trial in a new jurisdiction can access the same

judge intelligence that a BigLaw associate takes for granted.

A law student can do state trial court research without needing their school’s Westlaw

subscription.

A legal aid attorney can generate a case assessment and draft motion arguments

without spending hours they do not have.

An investigative journalist can query judicial patterns across an entire state.

Another legal tech developer can build on Judgemind’s API without asking permission.

Quantitative Milestones (aspirations, not promises)

Timeframe
Target

Month 6
CA tentative capture live. 3–5 states with docket ingestion. Alerts and

document tools functional. Hundreds of users.

Month 12
15–20 states. AI document tools mature. Judge profiles for high-volume CA

courts. Thousands of users.

Month 18
30+ states. CA judge analytics crossing usefulness thresholds for common

judges. AI motion drafting in production. Tens of thousands of users.

Month 24
40+ states. TX/NY analytics becoming useful for common judges. Genuine

Trellis alternative for budget-constrained users.

Approaching comprehensive coverage. CA analytics rivaling Trellis depth for

Month

high-volume judges. Full Trellis parity: 5+ years for low-volume courts; high-

36+

volume courts converge faster. Community contributions and court

partnerships may accelerate specific jurisdictions.

13. Risks

Updated in v2.0 with new risks identified during investigation.

Risk
Severity
Mitigation

Trellis moat applies primarily to CA tentatives. For other

states, compete on access, price (free), openness, and

features Trellis does not offer. Focus early on features that

Data moat is

insurmountable
High

do not require deep data (deposition prep, discovery tools,

workspaces, document AI). Our architectural knowledge lets

us build the capture pipeline fast even though the data itself

takes time.

Tyler

Technologies

Approach Tyler as partner first. Lead with public-interest

High

blocks access

mission. Respect TOS. Use human-speed scraping with

(NEW)

or takes legal

delays. Do not enumerate or bulk download. Legal counsel

action

review before any re:Search scraping.

Respect robots.txt. Approach courts as partners, not

Courts block

adversaries. Offer value (free permanent archival of their

scraping
Medium

rulings). Diversify scraping methods. Human-speed request

patterns with delays.

RAG grounding. Citation verification. Clear AI disclaimers.

AI hallucination

in legal context
High

Human review pipeline. Never present AI output as

authoritative legal analysis.

Hosting costs

Open source means others can self-host. Optimize AI costs

become

Medium

with open-source models. Accept donations or grants if

unsustainable

needed. Design for cost ceiling awareness from day one.

Start with a small advisory board of interested attorneys.

No human

Law school partnerships. Offer acknowledgment and credit.

reviewers

Medium

The open-source/free mission attracts idealistic

volunteer

practitioners.

Illinois access

Investigate post-COVID status. If still restrictive, deprioritize

policy blocks

Medium

IL and reallocate to states with better access. Consider

meaningful

(NEW)

policy advocacy.

data

Florida

fragmentation

Start with 5 largest counties. Identify CMS platform clusters

Medium

makes

for template reuse. Accept that full FL coverage will take

(NEW)

coverage

years.

uneconomical

Trellis drops

Low
Judgemind is free. There is no price war to lose. Trellis has

prices in

investors and employees to pay; they cannot match free.

response

Legal liability

Clear disclaimers everywhere. Not legal advice. AI-

for AI-

Medium

generated label on all outputs. Terms of service. Consult

generated

legal counsel on platform liability.

content

Appendix A: Trellis.law Intelligence Sources

This spec was built from the following public sources:

trellis.law — Homepage, Features (/features/ai), Pricing (/plans), Coverage, and API

(/legal-data-api) pages.

support.trellis.law — Knowledge base: coverage, alerts, dockets, judges, rulings,

verdicts, counsel/attorney research, subscription tiers, API documentation.

LawNext / Bob Ambrogi — Detailed review of Trellis AI launch, November 2024.

SF Bar Association — Trellis AI launch press release, November 2024.

GetApp, Capterra, LegalTechHub, SourceForge, SoftwareWorld, LawGaze — Feature

lists, pricing, reviews.

LinkedIn (Trellis Law company page) — Product updates, team posts, conference

appearances.

Trellis client testimonials from Venable LLP, Fisher Phillips, Bowman and Brooke, Harvard

Law School, Guerra Law, Clayton Trial Lawyers, Cleveland State, San Diego Law Library,

and others.

Appendix B: Investigation Sources (added in v2.0)

The state-by-state data landscape analysis in Section 2 was compiled from direct

investigation of the following sources during March 2026:

Court websites for all 5 priority states (CA, TX, FL, NY, IL)

Tyler Technologies re:Search portals: research.txcourts.gov, researchca.tylerhost.net,

researchil.tylerhost.net, researchga.tylerhost.net

NYSCEF (iapps.courts.state.ny.us/nyscef/) and eCourts portals

Florida Clerks (flclerks.com), Civitek Solutions (civiteksolutions.com), individual county

clerk portals

Judici (judici.com) for Illinois county court access

judyrecords.com (including their published account of the Tyler incident)

UniCourt (unicourt.com) documentation and coverage pages

CourtListener (courtlistener.com) and Free Law Project documentation

tentativerulings.org and rulings.law for California tentative ruling coverage

Trellis.law support documentation confirming tentative rulings are California-only

Los Angeles Superior Court, Orange County Superior Court, and other California county

court websites

Florida Supreme Court Administrative Order AOSC24-65

Illinois Supreme Court 2004 Electronic Access Policy

END OF DOCUMENT