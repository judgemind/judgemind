# Judgemind Brand Guide

## Logo

- **Full logo:** `logo.svg` — icon + wordmark, stacked vertically
- **Icon only:** `icon.svg` — mark on dark rounded square, for favicon/app icon
- The logo has no background fill; place on light surfaces (`#f5f5f4` or white)
- The icon uses a dark stone background with white mark; use as-is

## Wordmark

- Font: Inter (falling back to Helvetica, Arial, sans-serif)
- "judge" — weight 300 (light), color Stone 900 (`#292524`)
- "mind" — weight 600 (semi-bold), color Amber 700 (`#b45309`)
- Lowercase, no space between words

## Color Palette

### Primary Colors

| Name | Hex | Usage |
|------|-----|-------|
| Stone 900 | `#292524` | Primary text, headings, icon, dark backgrounds |
| Stone 700 | `#44403c` | Secondary text, body copy |
| Stone 500 | `#78716c` | Muted text, captions, placeholders |
| Stone 300 | `#d6d3d1` | Borders, dividers |
| Stone 100 | `#f5f5f4` | Surface/background |
| White | `#ffffff` | Card backgrounds, page background |

### Accent

These amber hexes all map to the `brand-accent` Tailwind token (see
§Tailwind Token Mapping below). **Never** write `text-accent`,
`bg-accent`, etc. as bare utilities to get this colour — that token
is a shadcn hover-surface gray. Use `text-brand-accent`.

| Name | Hex | Tailwind token | Usage |
|------|-----|----------------|-------|
| Amber 700 | `#b45309` | `brand-accent` (DEFAULT) | Primary accent — links, active states, CTAs, wordmark (light mode) |
| Amber 800 | `#92400e` | `brand-accent-hover` | Hover on brand-accent chrome |
| Amber 600 | `#d97706` | `brand-accent-light` | Primary accent on dark mode |
| Amber 500 | `#f59e0b` | `brand-accent-lighter` | Lighter accent — highlights, selected states |
| Amber 100 | `#fef3c7` | `brand-accent-surface` | Accent surface — subtle backgrounds, badges |

### Semantic

| Name | Hex | Usage |
|------|-----|-------|
| Green 700 | `#15803d` | Success, "granted" outcomes |
| Red 700 | `#b91c1c` | Error, destructive, "denied" outcomes |
| Stone 500 | `#78716c` | Neutral/muted outcomes |

### Using stone in code

The visual palette is stone, but `.tsx` code must **not** reference
Tailwind `stone-*` classes directly (e.g. `bg-stone-100`, `text-stone-700`).
Those classes bypass the theming layer and break dark mode.
Use the semantic tokens instead — they resolve to the correct stone shade in
light mode and shift automatically in dark mode via CSS custom properties in
`globals.css` (no per-component `dark:bg-stone-900` rules needed):

```tsx
// DO — semantic tokens (stone shade shown in comment)
<p className="text-foreground">…</p>          {/* stone-900 light / stone-50 dark */}
<p className="text-muted-foreground">…</p>    {/* stone-500 */}
<div className="bg-muted">…</div>             {/* stone-100 light / stone-800 dark */}
<div className="bg-secondary">…</div>         {/* stone-100 light / stone-800 dark */}
<div className="bg-card">…</div>              {/* white light / stone-950 dark */}
<div className="border-border">…</div>        {/* stone-300 light / stone-700 dark */}

// DON'T — raw stone classes fail CI the same way bg-gray-200 does
<div className="bg-stone-200">…</div>  // ✗ blocked by check-hardcoded-colors.sh
```

`scripts/check-hardcoded-colors.sh` (introduced in #1444) guards `slate`,
`gray`, `zinc`, `stone`, and `neutral` class names in `packages/web/src/**/*.tsx`.
A `bg-stone-200` in production code will fail CI.

## Design Principles

1. **Function first.** The chrome gets out of the way. Content — rulings, case numbers, judge names — is the product.
2. **Warm neutrals.** Stone tones feel professional without being cold. The amber accent adds life without competing.
3. **Minimal accent use.** Amber is reserved for interactive elements and the wordmark. If everything is amber, nothing is.
4. **Node aesthetic.** The graph-node style from the icon can echo in the UI — dot separators, node-style status indicators — but sparingly.

## Tailwind Token Mapping

> **Footgun — `accent` vs `brand-accent`.** shadcn's `accent` token
> (`hsl(var(--accent))` in `packages/web/tailwind.config.ts`) is a
> **near-gray hover surface**, not the brand amber. The brand amber
> lives on the separate `brand-accent` token. The two classes are one
> character apart — `text-accent` vs `text-brand-accent` — and the
> shadcn one is nearly invisible on both light and dark backgrounds.
>
> - **Use `brand-accent` for always-visible brand chrome** — links,
>   active states, wordmark accents. Canonical pattern:
>   `text-brand-accent dark:text-brand-accent-light` (see
>   `packages/web/src/components/Wordmark.tsx`).
> - **Use `accent` only in shadcn hover / selected / focused idioms** —
>   e.g. `hover:bg-accent hover:text-accent-foreground`,
>   `data-[selected=true]:bg-accent`. Never as a bare class on an
>   always-visible element.
>
> Regressions on `/admin/dispatcher` (#2816) prompted a CI guard that
> was expanded repo-wide in #2832
> (`scripts/check-bare-shadcn-accent.sh`); it blocks bare `*-accent`
> utilities anywhere under `packages/web/src/`. Legitimate selected-row
> surfaces (sidebar active nav, filter pills) waive the check with an
> inline `shadcn-accent: intentional` comment. When in doubt, grep the
> Wordmark component for the canonical brand-accent pattern.
>
> The same guard was generalised in #4225 to cover the broader
> invisible-chrome family: bare `(text|bg|border|ring)-X-foreground`
> for X in {`primary`, `secondary`, `card`, `popover`, `destructive`,
> `accent`} unless paired with the corresponding `(bg|text|border|ring)-X`
> surface on the same element, plus the symmetric `text-background` /
> `bg-foreground` token swap (white-on-white in light mode,
> near-black-on-near-black in dark mode). `text-muted-foreground` is
> allowlisted unconditionally — it is the legitimate body-color
> idiom (Stone 500 in §Color Palette). The same
> `shadcn-accent: intentional` marker waives violations across the
> full family.

```
// packages/web/tailwind.config.ts
colors: {
  stone: { /* Tailwind's built-in stone scale */ },

  // Brand amber — always-visible chrome (links, wordmark, emphasis).
  'brand-accent': {
    DEFAULT:    '#b45309',  // amber-700 — light-mode text / fill
    hover:      '#92400e',  // amber-800
    light:      '#d97706',  // amber-600 — dark-mode text / fill
    lighter:    '#f59e0b',  // amber-500
    surface:    '#fef3c7',  // amber-100 — badge / pill surface
    foreground: '#ffffff',
  },

  // shadcn hover-surface token — gray by default, used ONLY with a
  // modifier (hover:, focus:, data-[...]:). Never as bare `text-accent`.
  accent: {
    DEFAULT:    'hsl(var(--accent))',
    foreground: 'hsl(var(--accent-foreground))',
  },
}
```
