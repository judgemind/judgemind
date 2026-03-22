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

| Name | Hex | Usage |
|------|-----|-------|
| Amber 700 | `#b45309` | Primary accent — links, active states, CTAs, wordmark |
| Amber 600 | `#d97706` | Hover state for accent elements |
| Amber 500 | `#f59e0b` | Lighter accent — highlights, selected states |
| Amber 100 | `#fef3c7` | Accent surface — subtle backgrounds, badges |

### Semantic

| Name | Hex | Usage |
|------|-----|-------|
| Green 700 | `#15803d` | Success, "granted" outcomes |
| Red 700 | `#b91c1c` | Error, destructive, "denied" outcomes |
| Stone 500 | `#78716c` | Neutral/muted outcomes |

## Design Principles

1. **Function first.** The chrome gets out of the way. Content — rulings, case numbers, judge names — is the product.
2. **Warm neutrals.** Stone tones feel professional without being cold. The amber accent adds life without competing.
3. **Minimal accent use.** Amber is reserved for interactive elements and the wordmark. If everything is amber, nothing is.
4. **Node aesthetic.** The graph-node style from the icon can echo in the UI — dot separators, node-style status indicators — but sparingly.

## Tailwind Token Mapping

```
colors: {
  stone: { /* Tailwind's built-in stone scale */ },
  accent: {
    DEFAULT: '#b45309',  // amber-700
    hover: '#d97706',    // amber-600
    light: '#f59e0b',    // amber-500
    surface: '#fef3c7',  // amber-100
  },
}
```
