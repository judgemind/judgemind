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

### Pagination

All list pages use infinite scroll with a two-part pattern:

1. **`useInfiniteScroll` hook** (`packages/web/src/hooks/useInfiniteScroll.ts`) — handles the **data-fetching concern**: concurrency protection (prevents duplicate in-flight requests via a `fetchingMore` ref), generation tracking (discards stale results when filters change mid-flight), and the `fetchMore` call with merge logic.
2. **`InfiniteScrollTrigger` component** (`packages/web/src/components/InfiniteScrollTrigger.tsx`) — handles the **rendering concern**: manages the `IntersectionObserver` lifecycle, renders an invisible sentinel div, and invokes `onLoadMore` when the sentinel scrolls into view.

Do **not** duplicate `IntersectionObserver` logic inline or in hooks — always use `InfiniteScrollTrigger`. Do **not** write inline `fetchingMore` refs — always use `useInfiniteScroll`.

**Usage:**

```tsx
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll';
import { InfiniteScrollTrigger } from '@/components/InfiniteScrollTrigger';

// In the component body:
const { handleLoadMore } = useInfiniteScroll<MyData>({
  hasNextPage: pageInfo?.hasNextPage,
  endCursor: pageInfo?.endCursor,
  loading,
  fetchMore,
  merge: useCallback(
    (prev: MyData, incoming: MyData) => ({
      items: {
        ...incoming.items,
        edges: [...prev.items.edges, ...incoming.items.edges],
      },
    }),
    [],
  ),
  filterDeps: [county, dateFrom],  // optional — for filter-aware pages
});

// In JSX — place after the last list row:
<InfiniteScrollTrigger
  hasNextPage={pageInfo?.hasNextPage ?? false}
  loading={loading}
  onLoadMore={handleLoadMore}
/>
```

**Hook options (`useInfiniteScroll`):**

| Option | Type | Description |
|--------|------|-------------|
| `hasNextPage` | `boolean \| undefined` | Whether the server reports more pages available |
| `endCursor` | `string \| null \| undefined` | The cursor to pass as `after` in the next `fetchMore` call |
| `loading` | `boolean` | Whether a fetch is currently in progress (from `useQuery`) |
| `fetchMore` | `(options) => Promise` | The `fetchMore` function from Apollo's `useQuery` |
| `merge` | `(prev, incoming) => TData` | Merge function combining previous data with newly-fetched data |
| `filterDeps` | `DependencyList` (optional) | Dependency list for filter values — discards in-flight results on change |

**Component props (`InfiniteScrollTrigger`):**

| Prop | Type | Description |
|------|------|-------------|
| `hasNextPage` | `boolean` | Whether more data is available to load |
| `loading` | `boolean` | Whether a fetch is currently in progress |
| `onLoadMore` | `() => void` | Callback invoked when the sentinel enters the viewport |
| `rootMargin` | `string` (optional) | `IntersectionObserver` rootMargin for pre-fetching (default: `'200px'`) |

**Rules:**
- Never use "Load more" buttons. Pagination is always automatic via scroll.
- Always pair `useInfiniteScroll` with `InfiniteScrollTrigger`. The hook provides `handleLoadMore`; the component provides the observer and sentinel.
- Place the `<InfiniteScrollTrigger>` immediately after the last rendered row, inside the same container.
- The component renders an invisible 1px sentinel div and hides itself while loading to prevent duplicate fetches.

### Clickable Table Rows

Shadcn's `TableRow` component applies `hover:bg-muted/50` by default, which makes every row *look* clickable on hover. This is a misleading affordance unless the row itself is actually navigable. The same UX bug has been fixed reactively on the cases, rulings, and judges tables — this pattern prevents future regressions.

**Rule:** if a `TableRow` contains a navigation `<Link>` or `<a>`, the entire row must be clickable.

- Put `onClick={() => router.push(...)}` on the `TableRow`, with `cursor-pointer`.
- Put `onClick={(e) => e.stopPropagation()}` on any interactive elements inside the row — checkboxes, buttons, and the nested link itself (so cmd+click / right-click still open the link in a new tab without triggering the row's navigation).
- Header rows (`<TableRow>` inside `<TableHeader>`, i.e. rows containing only `<TableHead>` children) are not expected to be clickable — they can safely contain `<Link>` without a row-level `onClick`.

**Pattern (from `JudgesList.tsx`):**

```tsx
<TableRow
  key={node.id}
  className="cursor-pointer"
  onClick={() => router.push(`/judges/${node.id}`)}
>
  <TableCell className="w-10" onClick={(e) => e.stopPropagation()}>
    <Checkbox
      checked={isSelected}
      onCheckedChange={() => toggleSelection(node.id)}
      aria-label={`Select ${node.canonicalName}`}
    />
  </TableCell>
  <TableCell>
    <Link
      href={`/judges/${node.id}`}
      className="rounded-sm font-medium hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onClick={(e) => e.stopPropagation()}
    >
      {node.canonicalName}
    </Link>
  </TableCell>
  {/* ... */}
</TableRow>
```

**Enforcement:** an ESLint rule (`local/clickable-table-row`) flags any `<TableRow>` that contains a nested `<Link>` or `<a>` without an `onClick` handler on the row itself. See `packages/web/eslint-rules/clickable-table-row.js`.

**Alternative:** for list pages where every column is informational (no checkboxes, no inline buttons), prefer the `<Link>`-wrapped row pattern used in `CasesList` and `RulingsFeed` — the entire row is a `<Link>` with `className="block ..."`. This gives native right-click / cmd-click behavior without needing `stopPropagation` plumbing. The `TableRow` + `onClick` pattern is appropriate when the row contains selection checkboxes or other interactive controls that cannot live inside an `<a>` tag.

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
