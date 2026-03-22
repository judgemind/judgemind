# User Journeys

## Overview

These journeys describe how attorneys and other users interact with Judgemind. They are organized by task, not by user type — a solo practitioner and a public defender may both need to evaluate a peremptory challenge. The journeys roughly follow the lifecycle of a case, from intake through active litigation.

---

## Journey 1: Case Intake / Viability Assessment

**When:** An attorney is deciding whether to take a case.

**Goal:** Evaluate whether the case is likely winnable by looking at how similar cases have played out. "Similar" means same case type — slip and fall, lemon law, complex civil litigation involving a particular type of contract, etc.

**What they need:**
- Search or browse by case type / subject matter
- See motion-level outcomes: granted, denied, granted in part
- Understand how judges handle the motions that shape this type of case (e.g., do demurrers survive? do MSJs get granted?)
- Compare across judges and courts

**Note on outcomes:** Final case outcomes (win/loss) are mostly unavailable — the vast majority of cases settle, and settlement terms are not public record. The actionable signal is at the motion level: how judges rule on the motions that determine whether a case survives, proceeds to trial, or gets narrowed. A judge who consistently denies MSJs keeps cases alive; one who grants demurrers aggressively makes early survival harder.

**Key question:** "Is this case worth taking?"

---

## Journey 2: Jurisdiction Shopping

**When:** Before filing. The attorney has flexibility in where to file (e.g., LA and SF are both viable).

**Goal:** Compare how judges in different jurisdictions handle this type of case. Understand whether judges tend to favor plaintiffs (usually the little guy) or defendants (usually the big guy).

**What they need:**
- Per-jurisdiction breakdown of motion outcomes for a case type
- Judge-level tendencies within each jurisdiction (grant rates, plaintiff/defendant lean on motions)
- Enough data to make a statistically meaningful comparison
- Understanding of which judges are more or less favorable to plaintiffs at the motion stage

**Note:** "Plaintiff-friendly" and "defendant-friendly" are measured through motion outcomes, not case outcomes. A plaintiff-friendly judge denies defense MSJs and demurrers more often; a defendant-friendly judge grants them. Settlement rates and terms are not visible.

**Key question:** "Where should I file this?"

---

## Journey 3: Peremptory Challenge Decision

**When:** Early in a case, after a judge is assigned. An attorney has one opportunity to "ding" (peremptory challenge) the assigned judge, which causes a different judge to be randomly assigned. This is a one-time, irreversible strategic decision.

**Goal:** Decide whether to challenge the assigned judge. This requires understanding both the assigned judge AND the pool of likely replacement judges. Challenging a moderately unfavorable judge is a gamble — the replacement could be worse.

**What they need:**
- Assigned judge's tendencies on the relevant motion types
- Assigned judge's plaintiff/defendant lean
- Who the other judges in the same court/department are
- Those judges' tendencies for comparison
- Historical outcomes to gauge whether the assigned judge is an outlier or typical for this court

**Key question:** "Should I ding this judge, or is the risk of getting someone worse too high?"

---

## Journey 4: Topic Monitoring in a County

**When:** During active litigation. The attorney has a pending case and wants to stay aware of related activity.

**Goal:** Follow other cases in the same county on the same topic or case type. This surfaces relevant precedent, reveals how other judges are handling similar issues right now, and may inform strategy.

**What they need:**
- Filter rulings by county + case type or subject matter
- Ongoing feed or alerts for new rulings matching their criteria
- Enough context in the feed to decide whether a ruling is worth reading in full

**Key question:** "What else is happening like my case in this county?"

---

## Journey 5: Case Tracking

**When:** Anytime. An attorney finds a specific case interesting — either because it is professionally relevant or personally compelling.

**Goal:** Follow the case over time and see new rulings, filings, or developments as they happen.

**What they need:**
- Ability to find and bookmark a specific case
- Notifications or a feed when new activity occurs on the case
- Quick access to the latest ruling or filing

**Key question:** "What's happening in this case?"

---

## Journey 6: Person Background Search

**When:** Anytime. An attorney wants to understand what litigation a person or entity has been involved in.

**Goal:** Search by party name and see all cases involving that person — as plaintiff, defendant, or other role. Understand their litigation history.

**What they need:**
- Party name search across all cases
- Results grouped or filterable by role (plaintiff, defendant)
- Case type, court, outcome, and date range for each case
- Ability to distinguish between people with similar names

**Key question:** "What litigation has this person been involved in?"

---

## Journey 7: Motion Strategy

**When:** Mid-case. The attorney has an assigned judge and is preparing a specific motion (MSJ, demurrer, motion to compel, etc.).

**Goal:** Understand what arguments the assigned judge finds compelling. Not just win rates — the attorney wants to read the reasoning in rulings where the judge granted or denied similar motions, to tailor their brief accordingly.

**What they need:**
- Filter the judge's rulings by motion type
- Read full ruling text, especially the reasoning sections
- See which arguments succeeded vs failed
- Ideally, pattern recognition: "This judge consistently cites X standard" or "This judge is skeptical of Y argument"

**Key question:** "What arguments work with my judge?"

---

## Implications for the Product

### What these journeys have in common

- **Case type / subject matter is a primary axis.** Most journeys start with "cases like mine." The product needs robust categorization and search by case type.
- **Comparison is essential.** Attorneys rarely look at a single data point — they compare judges, jurisdictions, outcomes. The product should make comparison easy, not require manual cross-referencing.
- **Ruling text matters.** Win rates are a starting point, but attorneys need to read the actual reasoning to build strategy. Analytics alone aren't enough.
- **Time dimension matters.** Both historical (trends, enough data for statistical confidence) and real-time (what's happening now, alerts for new rulings).

### Capabilities required

| Capability | Journeys |
|------------|----------|
| Search/filter by case type or subject matter | 1, 2, 4 |
| Per-jurisdiction motion outcome comparison | 2, 3 |
| Judge tendency analytics (motion type, plaintiff/defendant lean) | 2, 3, 7 |
| Judge pool comparison within a court | 3 |
| Ruling text search and reading | 4, 7 |
| Case monitoring and alerts | 4, 5 |
| Party name search | 6 |
| Ruling reasoning analysis / argument extraction | 7 |
