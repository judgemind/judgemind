# Web UI Patterns

Standard page patterns for the Judgemind web app. Consult this when building or modifying any frontend page. These patterns exist to prevent divergence — every list page should feel like the same app, every detail page should feel like the same app.

See also:
- `docs/BRAND.md` — colors, typography, design principles
- `docs/specs/user-journeys.md` — who uses the app and how
- `docs/specs/product-spec-v3.md` — product requirements
- `docs/web-lessons.md` — frontend incident lessons

## Design Principles (from BRAND.md)

1. **Function first.** Chrome gets out of the way. Content is the product.
2. **Warm neutrals.** Stone tones, amber accent for interactive elements only.
3. **Minimal accent use.** If everything is amber, nothing is.
4. **Information density.** Maximize useful content per viewport. No decorative cards, no unnecessary wrappers.

## Page Types

The app has four page types. Every new page must follow one of these patterns.

### 1. List Page

**Purpose:** Browse and filter a collection of items (rulings, cases, judges).

**Anatomy:**
```
┌─ Breadcrumb (if nested) ─────────────────────────────┐
│ Page Title                                            │
│ Subtitle (one line, muted)                            │
│                                                       │
│ ┌─ Filter Bar ──────────────────────────────────────┐ │
│ │ [Search/text input]  [Filter]  [Filter]  [Filter] │ │
│ └────────────────────────────────────────────────────┘ │
│                                                       │
│ ┌─ Row ─────────────────────────────────────────────┐ │
│ │ Primary text          Secondary metadata    Date  │ │
│ │ Supporting detail     Badges                      │ │
│ └────────────────────────────────────────────────────┘ │
│ ┌─ Row ─────────────────────────────────────────────┐ │
│ │ ...                                               │ │
│ └────────────────────────────────────────────────────┘ │
│                                                       │
│ (infinite scroll — no "Load more" button)             │
└───────────────────────────────────────────────────────┘
```

**Rules:**
- **Filters** go in a horizontal bar above the list, not in a sidebar. Keep filters compact — dropdowns and pills, not checkbox lists.
- **Rows** are borderless, separated by a subtle divider (`border-border`). No card wrappers per row.
- **Rows are `<Link>` elements** — right-clickable, cmd+clickable for new tab. Never `onClick` navigation on a `<div>`.
- **Hover state** on rows: `hover:bg-stone-50` (or `hover:bg-muted/50`).
- **Pagination** is infinite scroll via intersection observer. Never use "Load more" buttons.
- **Information per row** must include enough context to decide whether to click — at minimum: title, date, and the most important metadata (e.g., outcome for rulings, ruling count for cases).
- **Consistent columns/fields** across list pages that show the same data type. If two list pages show rulings, they show the same fields in the same order.

### 2. Detail Page

**Purpose:** View a single item in full (a ruling, a case, a judge).

**Anatomy:**
```
┌─ Breadcrumb ──────────────────────────────────────────┐
│                                                       │
│ Primary Identifier (bold, large)    [Type Badge]      │
│ Title / description (muted)                           │
│ Metadata line: court · county · judge · dept          │
│                                                       │
│ ┌─ Context Section (if applicable) ────────────────┐  │
│ │ Parties, related entities, analytics summary     │  │
│ └──────────────────────────────────────────────────┘  │
│                                                       │
│ ┌─ Primary Content ────────────────────────────────┐  │
│ │ The main thing the user came to see              │  │
│ │ (ruling text, ruling list, analytics table)      │  │
│ └──────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────┘
```

**Rules:**
- **Header** is compact — one block with identifier, title, and metadata. No separate cards for individual metadata fields (no standalone "Department" card, no "Judge" card).
- **Metadata** uses a single line with dot separators: `Superior Court, County of Los Angeles · Dept. 32 · Judge Smith`. Not separate labeled fields.
- **Context before content.** Parties, case type, and other framing information appear before the primary content, so the user has context while reading.
- **Primary content fills the viewport.** The header and context section should not push primary content below the fold on a standard 1080p display.
- **Cross-linking.** Detail pages link to related entities: ruling detail links to its case, case detail links to its judge, judge detail links to their rulings.
- **No redundant information.** If the parent entity (case) shows the judge, child entities (rulings) do not repeat the judge unless it differs. Same for department, court, etc.

### 3. Search Page

**Purpose:** Find items across the entire dataset with filters.

**Anatomy:**
```
┌─ Page Title ──────────────────────────────────────────┐
│ Subtitle                                              │
│                                                       │
│ ┌─ Search Bar (full width, prominent) ─────── [Go] ┐ │
│ └──────────────────────────────────────────────────┘  │
│                                                       │
│ ┌─ Filter Bar (horizontal, compact) ───────────────┐  │
│ │ [County ▾]  [Judge ▾]  [Motion ▾]  [Outcome ▾]  │  │
│ └──────────────────────────────────────────────────┘  │
│                                                       │
│ Results (same row format as list pages)               │
│ ...                                                   │
└───────────────────────────────────────────────────────┘
```

**Rules:**
- **Search bar is the hero.** Largest interactive element on the page.
- **Filters are secondary.** Horizontal bar below the search input, collapsed or hidden when there are no results. Never a sidebar that competes with results.
- **Empty state** is centered, clear, and inviting — not buried next to a filter panel.
- **Error state** uses warm, muted styling (`text-red-700` on `bg-red-50` or `bg-stone-50`). Never a full-width alarming red banner.
- **Results use the same row component** as the corresponding list page. Search results for rulings look identical to rows on `/rulings`.

### 4. Profile/Analytics Page

**Purpose:** Show aggregate information about an entity (judge profile).

**Anatomy:**
```
┌─ Breadcrumb ──────────────────────────────────────────┐
│                                                       │
│ Entity Name                                           │
│ Metadata line: court · county                         │
│                                                       │
│ ┌─ Stats Bar ──────────────────────────────────────┐  │
│ │ Stat 1    Stat 2    Stat 3    Date range         │  │
│ └──────────────────────────────────────────────────┘  │
│                                                       │
│ ┌─ Analytics Section ──────────────────────────────┐  │
│ │ Breakdown table / charts                         │  │
│ │ (rows are interactive — click to filter below)   │  │
│ └──────────────────────────────────────────────────┘  │
│                                                       │
│ ┌─ Related Items List ─────────────────────────────┐  │
│ │ (same row format as list pages, filterable)      │  │
│ └──────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────┘
```

**Rules:**
- **Analytics rows are interactive.** Clicking a row in the breakdown table filters the related items list below.
- **Stats use large, readable numbers.** Not buried in body text.
- **Related items list** uses the same row component as the corresponding list page.

## Component Patterns

### Badges

Badges communicate categorical information at a glance. Use consistently across all surfaces.

**Outcome badges** (semantic colors from BRAND.md):
- Granted: `text-green-700`, bordered pill
- Denied: `text-red-700`, bordered pill
- Granted in Part / Partial: `text-green-700`, bordered pill
- Moot / Continued / Other: `text-stone-500`, bordered pill

**Type badges** (neutral):
- Case type (Civil, Family, etc.): `text-stone-600`, light bordered pill
- Status (Tentative): muted/ghost style only — `text-stone-400`, no background. Do not give it the same visual weight as outcome badges.

**Rule:** Outcome badges are always more visually prominent than type/status badges. Use a single shared badge component everywhere.

### Metadata Lines

Use inline dot-separated metadata for compact display:

```
Superior Court, County of Los Angeles · Dept. 32
Judge Armenui A. Ashvanian
```

Not:
```
COURT          DEPARTMENT     JUDGE
Superior...    32             Armenui...
```

Labeled fields waste space. Reserve tabular layout for data that benefits from column alignment (analytics tables, comparison views).

### Empty States

- Centered in the content area
- Muted icon (optional) + clear message + suggested action
- Never compete with navigation or filter chrome

### Loading States

- Skeleton loaders matching the shape of the content they replace
- "Loading…" text uses actual ellipsis character (`…`), never `\u2026`

### Error States

- Soft styling: `text-red-700` on `bg-red-50` or inline with `text-muted-foreground`
- Include a retry action
- Never full-width alarming red banners

## Visual Hierarchy Within Rows

Every list row has a clear scanning order:

1. **Primary** — the thing you're scanning for (case title, judge name). Bold, `text-foreground`, largest.
2. **Key metadata** — the most important secondary info (outcome badge, grant rate). Visually prominent but smaller than primary.
3. **Supporting** — context that helps decide whether to click (court, judge, motion type). `text-muted-foreground`, smaller.
4. **Tertiary** — timestamp, status. `text-muted-foreground`, right-aligned or smallest.

If everything in a row has the same visual weight, the hierarchy is broken.

## Responsive Behavior

- **Mobile (< 768px):** Sidebar collapses to hamburger/bottom nav. Full content width. Table rows may stack into card-like layouts.
- **Tablet (768px–1024px):** Consider icon-only sidebar. Filters may wrap to multiple lines.
- **Desktop (> 1024px):** Full sidebar. Horizontal filter bar. Wide content area.

Touch targets: minimum 44x44px on mobile.

## Consistency Rules

These rules prevent the divergence problem:

1. **Same data, same component.** If two pages show rulings, they use the same row component. No "ruling row style A" on one page and "ruling row style B" on another.
2. **Same filters everywhere.** If county filtering exists on one list page, it exists on all list pages that show county-scoped data.
3. **No information hiding.** If a list page shows items that have outcomes, show the outcomes. Don't make the user click into a detail page to see the most important field.
4. **Detail pages show context.** A ruling detail page shows case context. A case detail page lets you read rulings. Don't force navigation back and forth.
5. **One badge style per concept.** Outcomes look the same everywhere. Case types look the same everywhere. Motion types look the same everywhere.
