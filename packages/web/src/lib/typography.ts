/**
 * Shared typography constants for the three-level heading hierarchy.
 *
 * These constants define a consistent typographic scale across all pages.
 * Using them prevents style drift — every page automatically gets the
 * correct heading styles without copying raw Tailwind class strings.
 *
 * Hierarchy (established in #1450):
 *   1. PAGE_TITLE  — primary page heading (e.g., "Latest Rulings", "Judges")
 *   2. SECTION_HEADING — secondary heading within a page (e.g., "Recent rulings", "How it works")
 *   3. SECTION_LABEL — small uppercase label for subsections (e.g., "Filters", field labels)
 */

/** Primary page heading — large, bold, tight tracking. */
export const PAGE_TITLE = 'text-2xl font-bold tracking-tight text-foreground';

/** Secondary heading within a page — medium, semibold. */
export const SECTION_HEADING = 'text-lg font-semibold text-foreground';

/** Small uppercase label for subsections or metadata fields. */
export const SECTION_LABEL =
  'text-sm font-semibold uppercase tracking-wide text-muted-foreground';
