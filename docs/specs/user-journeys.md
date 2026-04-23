# User Journeys

## Overview

These journeys describe how attorneys and other users interact with Judgemind. They are organized by task, not by user type — a solo practitioner and a public defender may both need to evaluate a peremptory challenge. The journeys roughly follow the lifecycle of a case, from intake through active litigation.

Each journey includes:
- **Stages** — the steps the user goes through
- **Touchpoints** — where interaction happens (Judgemind features, external systems, or human conversations)
- **Emotional arc** — how the user feels at each stage
- **Pain points** — where friction occurs today
- **Opportunities** — where the product can reduce friction or create new value
- **Metrics** — how we measure whether each stage is working

### Current state legend

Throughout the journey maps, touchpoints and capabilities are marked:

- **Built** — exists in the current product
- **Partial** — some support exists but incomplete
- **Gap** — not yet built

---

## Journey 1: Case Intake / Viability Assessment

**When:** An attorney is deciding whether to take a case.

**Goal:** Evaluate whether the case is likely winnable by looking at how similar cases have played out. "Similar" means same case type — slip and fall, lemon law, complex civil litigation involving a particular type of contract, etc.

**Key question:** "Is this case worth taking?"

### Stages

| | Identify Case Type | Search for Precedent | Analyze Outcomes | Form Opinion | Decide |
|---|---|---|---|---|---|
| **Actions** | Classify the potential case by subject matter and motion types that will matter | Search Judgemind for rulings in similar cases | Review motion outcomes — grant rates for MSJs, demurrers, etc. across judges | Synthesize: how do cases like this typically play out? | Accept or decline the case |
| **Touchpoints** | Client intake call, case documents | Search page (Built), rulings feed (Built) | Judge profiles with analytics (Built), ruling detail pages (Built) | Internal notes, co-counsel discussion | Client communication |
| **Emotions** | 😐 Neutral — routine intake process | 😐→😤 Uncertain — will I find relevant data? | 😊→😤 Depends on data quality and coverage | 😰 Anxious — am I reading this right? | 😊 Confident or 😐 Still uncertain |
| **Pain points** | No way to search by case type/subject matter (Gap) — attorney must guess which motion types map to their case | Search is keyword-only; no semantic understanding of case types | Stats exist per-judge but no cross-judge comparison view for a case type; hard to see "how do MSJs go across all judges in this county?" | No synthesis — attorney must manually aggregate across multiple judge profiles | If data coverage is thin, attorney doesn't know if absence of data means "no cases" or "we don't have the data" |
| **Opportunities** | Case type taxonomy and guided intake: "What kind of case is this?" | Case-type-aware search that surfaces relevant motion types automatically | Aggregate analytics by case type across judges/courts — "MSJ grant rate for slip-and-fall in LA County" | AI-generated case type briefing: "Here's how cases like yours typically play out in this jurisdiction" | Data coverage indicator: "We have N rulings for this case type in this county (high/medium/low confidence)" |
| **Metrics** | N/A (external) | Searches per session, zero-result rate by case type | Judge profiles viewed per search session, time on analytics page | N/A (internal to user) | Return visits within 7 days (proxy for "came back to verify") |

### Emotional arc
```
Intake        Search       Analyze      Synthesize    Decide
  😐     →    😐/😤    →    😊/😤    →     😰      →   😊/😐
Routine     Uncertain    Data-dependent  Anxious     Resolved
```

### Critical moment
**Search → Analyze.** If the attorney searches and finds nothing relevant — or finds data but can't filter by case type — they'll conclude the tool isn't useful for intake decisions and won't come back. The case type taxonomy is the unlock.

**Note on outcomes:** Final case outcomes (win/loss) are mostly unavailable — the vast majority of cases settle, and settlement terms are not public record. The actionable signal is at the motion level: how judges rule on the motions that determine whether a case survives, proceeds to trial, or gets narrowed. A judge who consistently denies MSJs keeps cases alive; one who grants demurrers aggressively makes early survival harder.

---

## Journey 2: Jurisdiction Shopping

**When:** Before filing. The attorney has flexibility in where to file (e.g., LA and SF are both viable).

**Goal:** Compare how judges in different jurisdictions handle this type of case. Understand whether judges tend to favor plaintiffs (usually the little guy) or defendants (usually the big guy).

**Key question:** "Where should I file this?"

### Stages

| | Identify Filing Options | Research Each Jurisdiction | Compare Side-by-Side | Evaluate Plaintiff/Defendant Lean | Decide Where to File |
|---|---|---|---|---|---|
| **Actions** | Determine which counties/courts have jurisdiction | Look up judge analytics in each viable jurisdiction | Compare motion outcome rates across jurisdictions | Assess which jurisdiction favors their client's position | File in chosen jurisdiction |
| **Touchpoints** | Legal analysis (external), case documents | Judge list (Built), judge profiles (Built) | Manual tab-switching between judge profiles (Partial) | Judge analytics — grant rates by motion type (Built) | Court filing system (external) |
| **Emotions** | 😐 Strategic — this is a deliberate choice | 😐 Methodical — gathering data | 😤 Tedious — lots of manual comparison | 😰 Uncertain — is the sample size big enough? | 😊 Confident or 😤 Best-guess |
| **Pain points** | No way to see "all jurisdictions where I could file this type of case" | Must navigate to each county's judges individually; no jurisdiction-level aggregate | No comparison view — attorney opens multiple tabs and cross-references manually | Plaintiff/defendant lean isn't surfaced explicitly; attorney must infer from grant/deny rates on defense motions | No confidence indicator for small sample sizes — 3 rulings looks the same as 300 |
| **Opportunities** | Jurisdiction selector: "Compare these courts for [case type]" | County-level analytics dashboard — aggregate stats without needing to visit each judge | Side-by-side jurisdiction comparison view with key metrics | Explicit plaintiff/defendant lean score derived from motion outcomes, with methodology explanation | Statistical confidence indicator: "Based on N rulings (high/low confidence)" |
| **Metrics** | N/A (external) | Counties browsed per session | Judge profiles viewed across multiple counties in one session (proxy for comparison behavior) | Time spent on analytics sections | Repeat visits to same county set (iterating on decision) |

### Emotional arc
```
Options      Research     Compare      Evaluate      File
  😐     →     😐     →    😤      →     😰      →   😊/😤
Strategic   Methodical   Tedious     Uncertain     Resolved
```

### Critical moment
**Research → Compare.** The product currently forces manual cross-referencing. Every extra tab the attorney has to open increases the chance they give up and ask a colleague instead. A jurisdiction comparison view would collapse this from a 30-minute task to a 2-minute task.

**Note:** "Plaintiff-friendly" and "defendant-friendly" are measured through motion outcomes, not case outcomes. A plaintiff-friendly judge denies defense MSJs and demurrers more often; a defendant-friendly judge grants them. Settlement rates and terms are not visible.

---

## Journey 3: Peremptory Challenge Decision

**When:** Early in a case, after a judge is assigned. An attorney has one opportunity to "ding" (peremptory challenge) the assigned judge, which causes a different judge to be randomly assigned. This is a one-time, irreversible strategic decision.

**Goal:** Decide whether to challenge the assigned judge. This requires understanding both the assigned judge AND the pool of likely replacement judges. Challenging a moderately unfavorable judge is a gamble — the replacement could be worse.

**Key question:** "Should I ding this judge, or is the risk of getting someone worse too high?"

### Stages

| | Learn of Assignment | Research Assigned Judge | Compare Replacement Pool | Assess Risk | Decide |
|---|---|---|---|---|---|
| **Actions** | Read assignment notice, note judge name | Look up judge on Judgemind — tendencies, motion outcomes, ruling text | Identify other judges in same court, compare their tendencies | Weigh: is the assigned judge bad enough to risk a random replacement? | File peremptory challenge or accept assignment |
| **Touchpoints** | Court notice / case management system (external) | Judge profile with analytics (Built), judge rulings list (Built) | Judge list filtered by county (Built), individual judge profiles (Built) | Internal discussion, co-counsel, possibly senior partner | Court filing system (external) |
| **Emotions** | 😐 Uncertain — don't know this judge | 😤→😊 Depends on what they find | 😰 Anxious — replacement could be worse | 😤 High-pressure — irreversible, time-limited | 😐 Resolved, for better or worse |
| **Pain points** | Clock is ticking — peremptory challenge deadlines are strict | Stats lack case-type context: "70% grant rate on MSJs" means different things for different case types | No "court pool" view — must manually visit each judge profile in the county and mentally compare; no way to see "where does my judge rank among peers?" | No framework for risk assessment — attorney must build expected value calculation in their head | No way to track the outcome (was the replacement better or worse?) for learning |
| **Opportunities** | Push notification when a judge is assigned to a tracked case | Pre-built "judge brief" — key stats + notable rulings filtered to the attorney's case type, ready in seconds | Court pool comparison view: ranked list of all judges in the court with key metrics, showing where the assigned judge falls | Challenge risk calculator: expected value analysis based on pool statistics — "If you ding Judge X, your expected grant rate on MSJs across the remaining pool is Y%" | Outcome tracking: "You challenged Judge X and got Judge Y — here's how Y compares" |
| **Metrics** | Time from assignment to first Judgemind lookup | Judge profile views within 24h of assignment (if trackable) | Number of judge profiles viewed in same court in one session | N/A (internal to user) | Challenge rate among users who viewed pool comparison (long-term) |

### Emotional arc
```
Assignment    Research     Compare      Assess       Decide
   😐    →    😊/😤    →    😰     →    😤      →    😐
Uncertain   Informed    Anxious    Pressured    Resolved
```

### Critical moment
**Compare → Assess.** This is where the product either earns trust or loses it. If the attorney has to manually cross-reference 12 judge profiles, they'll fall back to asking colleagues instead. A single comparison view that answers "is my judge an outlier?" is the difference between Judgemind being essential vs. optional for this high-stakes decision.

---

## Journey 4: Topic Monitoring in a County

**When:** During active litigation. The attorney has a pending case and wants to stay aware of related activity.

**Goal:** Follow other cases in the same county on the same topic or case type. This surfaces relevant precedent, reveals how other judges are handling similar issues right now, and may inform strategy.

**Key question:** "What else is happening like my case in this county?"

### Stages

| | Define Interest | Set Up Monitoring | Review New Rulings | Evaluate Relevance | Apply to Own Case |
|---|---|---|---|---|---|
| **Actions** | Identify the case type, motion types, and county to track | Filter the rulings feed or set up alerts | Scan new rulings matching criteria | Read full ruling text for relevant ones | Incorporate insights into own case strategy |
| **Touchpoints** | Internal case analysis | Rulings feed with county filter (Built), alerts (Gap) | Rulings feed (Built), email/push notifications (Gap) | Ruling detail page (Built), full ruling text (Built) | Internal notes, brief drafting |
| **Emotions** | 😐 Focused — knows what they're looking for | 😤 Frustrated if no alert system — must remember to check manually | 😐 Routine — scanning headlines | 😊 Excited when finding relevant ruling, 😐 mostly noise | 😊 Strategic advantage — or 😐 nothing useful |
| **Pain points** | No case type filter on the rulings feed — can only filter by county and date | No saved filters or alerts — must manually revisit and re-apply filters each time | Feed shows all rulings in the county; no way to narrow to a topic — high noise-to-signal ratio | Ruling excerpts in the feed may not show enough to assess relevance without clicking through | No way to save or annotate interesting rulings for later reference |
| **Opportunities** | Case type / subject matter filter on rulings feed | Saved filters with optional email/push alerts: "Notify me when a new [motion type] ruling is posted in [county]" | AI-powered relevance scoring: surface rulings most similar to the attorney's case | Expanded ruling previews with key reasoning highlighted | Bookmarking / collections: save rulings to a case-specific folder |
| **Metrics** | Filter usage on rulings feed | Alert subscription rate, saved filter count | Feed return visit frequency (daily, weekly) | Click-through rate from feed to ruling detail | Bookmarks per user (if built) |

### Emotional arc
```
Define       Set Up       Review       Evaluate     Apply
  😐     →    😤      →     😐     →    😊/😐    →   😊/😐
Focused    Frustrated    Scanning    Hit-or-miss   Strategic
```

### Critical moment
**Define → Set Up.** Attorneys are busy. If monitoring requires manually checking the feed and re-applying filters, most won't do it consistently. The gap between "I want to follow this" and "it's too much friction to follow this" is where users drop off. Saved filters with alerts would close this gap entirely.

---

## Journey 5: Case Tracking

**When:** Anytime. An attorney finds a specific case interesting — either because it is professionally relevant or personally compelling.

**Goal:** Follow the case over time and see new rulings, filings, or developments as they happen.

**Key question:** "What's happening in this case?"

### Stages

| | Discover Case | View Current State | Follow Over Time | Get Updates | Review Developments |
|---|---|---|---|---|---|
| **Actions** | Find the case via search, feed, or external reference | Read case details, parties, rulings to date | Bookmark or subscribe to the case | Receive notification when something new happens | Read new ruling or filing |
| **Touchpoints** | Search (Built), rulings feed (Built), external link | Case detail page (Built) — shows parties, judges, rulings | Bookmarking (Gap), case subscription (Gap) | Email/push notification (Gap) | Ruling detail page (Built) |
| **Emotions** | 😐 Curious | 😊 Informed — good case detail page | 😤 Frustrated — no way to save or follow | 😤 Must manually check back | 😊 Satisfied when found, 😤 if missed an update |
| **Pain points** | Search is keyword-only; finding a specific case requires knowing the case number or a distinctive party name | Case detail page exists and works well — minor pain point: no timeline/chronological view of all activity | No bookmark or follow feature — cannot save a case for later | No notification system — must manually revisit the case page to check for updates | If the attorney forgets to check, they may miss time-sensitive developments |
| **Opportunities** | Case search improvements: search by party name, case number, or topic | Case timeline view: chronological activity log showing all rulings and filings | "Follow this case" button — one-click to add to a personal watchlist | Email digest or push notification when a followed case gets a new ruling | Watchlist dashboard: all followed cases with latest activity, sorted by recency |
| **Metrics** | Case detail page views from search vs. direct link | Time on case detail page | N/A (feature doesn't exist yet) | N/A (feature doesn't exist yet) | Return visits to same case (proxy for manual tracking behavior) |

### Emotional arc
```
Discover     View        Follow       Updates      Review
  😐     →    😊    →     😤      →     😤     →    😊/😤
Curious    Informed   No save option  No alerts   Manual check
```

### Critical moment
**View → Follow.** The case detail page is solid — attorneys find what they need. But there's no bridge to "I want to keep watching this." Every case view that doesn't convert to a follow is a missed retention opportunity. This is the simplest high-impact feature gap: a "follow" button.

---

## Journey 6: Person Background Search

**When:** Anytime. An attorney wants to understand what litigation a person or entity has been involved in.

**Goal:** Search by party name and see all cases involving that person — as plaintiff, defendant, or other role. Understand their litigation history.

**Key question:** "What litigation has this person been involved in?"

### Stages

| | Identify Target | Search by Name | Review Results | Analyze History | Draw Conclusions |
|---|---|---|---|---|---|
| **Actions** | Identify the person or entity to research | Search Judgemind by party name | Scan results — filter by role, case type, date | Read case details and rulings for relevant cases | Form opinion on the person's litigation profile |
| **Touchpoints** | Client intake, opposing counsel notice, court records | Search page (Partial — keyword search exists, but no party-specific search) | Search results (Partial — may find cases mentioning the name in ruling text) | Case detail pages (Built), ruling detail pages (Built) | Internal notes |
| **Emotions** | 😐 Investigative | 😤 Frustrated — no dedicated party search | 😤→😐 Results are noisy; hard to tell if mentions are the right person | 😐 Methodical — reading through cases | 😊 Informed or 😤 Inconclusive |
| **Pain points** | N/A | No party name search — keyword search may surface ruling text mentioning the name, but not structured party data; cases list has a basic local filter but no server-side party search | Cannot filter by party role (plaintiff vs. defendant); results include any mention of the name, not just cases where the person is a party | No way to distinguish "John Smith the contractor" from "John Smith the doctor" — entity disambiguation is absent | No aggregate view: "involved in N cases, mostly as defendant, primarily personal injury" |
| **Opportunities** | N/A | Structured party search: search the parties table, not just ruling text | Role filter: show only cases where the person is plaintiff, defendant, or a specific role | Entity disambiguation: cluster by co-parties, case type, or court to help distinguish same-name individuals | Litigation profile summary: automated overview of a person's case history — types, frequency, outcomes, courts |
| **Metrics** | N/A | Party-name searches per session (if trackable via query patterns) | Results pages viewed per search | Case detail click-throughs from party search results | N/A (internal to user) |

### Emotional arc
```
Identify     Search       Review       Analyze      Conclude
  😐     →    😤      →    😤/😐    →    😐      →   😊/😤
Investigative  No party   Noisy      Methodical   Informed/
               search     results                 Inconclusive
```

### Critical moment
**Search → Review.** Party search is the most significant feature gap for this journey. Without structured party search, the attorney is doing what they could do in any search engine — keyword matching. Structured party data exists in the database (case detail pages show parties); it just isn't exposed as a search axis. This is a "wiring" problem, not a data problem.

---

## Journey 7: Motion Strategy

**When:** Mid-case. The attorney has an assigned judge and is preparing a specific motion (MSJ, demurrer, motion to compel, etc.).

**Goal:** Understand what arguments the assigned judge finds compelling. Not just win rates — the attorney wants to read the reasoning in rulings where the judge granted or denied similar motions, to tailor their brief accordingly.

**Key question:** "What arguments work with my judge?"

### Stages

| | Identify the Motion | Pull Judge's Track Record | Read Relevant Rulings | Extract Patterns | Draft Brief |
|---|---|---|---|---|---|
| **Actions** | Determine which motion to file and the legal standards involved | Look up the judge's rulings filtered by motion type | Read full ruling text for granted and denied motions of the same type | Identify recurring reasoning — what arguments does the judge find persuasive? | Incorporate patterns into the brief |
| **Touchpoints** | Legal research (external), case strategy | Judge profile (Built) — analytics show motion type breakdown; judge rulings list (Built) | Ruling detail page with full text (Built) | Manual note-taking (external); AI summary (Partial — exists but not argument-focused) | Brief drafting tools (external) |
| **Emotions** | 😐 Focused — clear task | 😊 Encouraged — grant rates give a quick read | 😐→😊 Productive — reading real rulings is valuable | 😤 Time-consuming — manually reading dozens of rulings to spot patterns | 😊 Strategic — armed with judge-specific insights |
| **Pain points** | N/A | Grant rates exist but lack context: "60% grant rate on MSJs" doesn't say what arguments drove the grants vs. denials | Full ruling text is available, but no way to surface the most relevant passages — must read entire rulings | Pattern extraction is entirely manual — no automated identification of recurring arguments, cited standards, or reasoning patterns | No way to export or organize findings — attorney copies and pastes into their own notes |
| **Opportunities** | N/A | Contextual grant rates: break down by argument type or legal standard, not just motion type | Ruling text search within a judge's rulings: "Show me where Judge X discusses 'triable issue of material fact'" | AI-powered argument analysis: "This judge cites the Aguilar standard in 80% of MSJ grants" / "This judge is skeptical of declarations lacking personal knowledge" | Export judge brief: one-page summary of the judge's tendencies on this motion type, with key quotes from rulings |
| **Metrics** | N/A | Judge profile views filtered to specific motion types | Ruling detail views per judge profile visit (depth of research) | Time on ruling detail pages (proxy for close reading) | Return visits to same judge before a filing deadline |

### Emotional arc
```
Identify     Track Record   Read Rulings   Extract      Draft
  😐     →      😊      →     😐/😊    →    😤      →   😊
Focused    Encouraged    Productive    Time-consuming  Strategic
```

### Critical moment
**Read Rulings → Extract Patterns.** This is where the attorney spends the most time and where AI can create the most value. Reading 30 rulings to manually spot patterns is exactly the kind of work that LLMs excel at. An argument analysis feature would compress hours of reading into a concise, judge-specific briefing — and this is something no competitor currently offers.

---

## Cross-Journey Analysis

### Pain point priority matrix

| Pain Point | Journeys Affected | Impact | Frequency | Current State | Priority |
|---|---|---|---|---|---|
| No case type / subject matter taxonomy | 1, 2, 4 | High | Every session | Gap | **P1** |
| No cross-judge or cross-jurisdiction comparison view | 2, 3 | High | Common | Gap | **P1** |
| No saved filters or alerts | 4, 5 | High | Daily use pattern | Gap | **P1** |
| No party name search | 6 | High | Common | Gap | **P2** |
| No court pool / peer comparison for judges | 3 | High | Per-case decision | Gap | **P2** |
| No AI argument/reasoning extraction | 7 | High | Per-motion prep | Gap | **P2** |
| No case bookmarking / following | 5 | Medium | Ongoing | Gap | **P2** |
| Statistical confidence indicators | 1, 2, 3 | Medium | Every analytics view | Gap | **P3** |
| Plaintiff/defendant lean score | 2, 3 | Medium | Common | Gap | **P3** |
| No entity disambiguation for party search | 6 | Medium | Occasional | Gap | **P3** |

### Emotional patterns across journeys

**Where users feel best (😊):**
- Reading a judge profile with analytics — the data is there and it's clear
- Reading full ruling text — this is genuinely valuable and unique
- Case detail pages — well-structured, informative

**Where users feel worst (😤):**
- Manual comparison across multiple tabs (Journeys 2, 3)
- No way to save, follow, or get alerts (Journeys 4, 5)
- Searching for something the data model supports but the UI doesn't expose (Journey 6)
- Manual pattern extraction from dozens of rulings (Journey 7)

**Common theme:** The data is often there. The friction is in surfacing, comparing, and monitoring it. The product's biggest gap isn't data — it's workflow.

### Capabilities required

| Capability | Journeys | Current State |
|---|---|---|
| Search/filter by case type or subject matter | 1, 2, 4 | Gap |
| Per-jurisdiction motion outcome comparison | 2, 3 | Gap |
| Judge tendency analytics (motion type, grant rates) | 2, 3, 7 | Built |
| Judge pool comparison within a court | 3 | Gap |
| Ruling text search and reading | 4, 7 | Built |
| Case monitoring and alerts | 4, 5 | Gap |
| Party name search | 6 | Gap |
| Ruling reasoning analysis / argument extraction | 7 | Gap |
| Saved filters / bookmarks | 4, 5 | Gap |
| Statistical confidence indicators | 1, 2, 3 | Gap |
| Data coverage transparency | 1 | Gap |

### What these journeys have in common

- **Case type / subject matter is a primary axis.** Most journeys start with "cases like mine." The product needs robust categorization and search by case type.
- **Comparison is essential.** Attorneys rarely look at a single data point — they compare judges, jurisdictions, outcomes. The product should make comparison easy, not require manual cross-referencing.
- **Ruling text matters.** Win rates are a starting point, but attorneys need to read the actual reasoning to build strategy. Analytics alone aren't enough.
- **Time dimension matters.** Both historical (trends, enough data for statistical confidence) and real-time (what's happening now, alerts for new rulings).
- **Data quality is the foundation.** Many of the UX opportunities above depend on having clean, complete, and correctly extracted data. Investing in scraper reliability, field completeness, and entity resolution pays dividends across every journey — comparison views and analytics are only as trustworthy as the data behind them.
