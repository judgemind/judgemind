'use client';

/**
 * Shared tiny components for the dispatcher admin page (#2805).
 *
 * All of these are single-purpose, monospace-where-appropriate, and
 * deliberately flat (no cards, no borders) — they render inside rows
 * that already have their own visual structure.
 *
 * Kept in one file because each component is ~10 lines and a separate
 * file-per-component would dilute the density-first design.
 */

const REPO = 'judgemind/judgemind';

// Brand amber token for hot links. Note: `text-accent` is shadcn's
// hover-surface gray, NOT the brand amber — see docs/BRAND.md §Tailwind
// Token Mapping and issue #2816. The canonical pattern lives in
// `packages/web/src/components/Wordmark.tsx`.
const BRAND_LINK_CLASSES =
  'font-mono text-brand-accent dark:text-brand-accent-light underline-offset-2 hover:underline';

/** Hot link to a GitHub issue. Opens in a new tab. */
export function IssueLink({ number, className }: { number: number; className?: string }) {
  const href = `https://github.com/${REPO}/issues/${number}`;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`${BRAND_LINK_CLASSES} ${className ?? ''}`}
      data-testid={`issue-link-${number}`}
    >
      #{number}
    </a>
  );
}

/** Hot link to a GitHub pull request. Opens in a new tab. */
export function PRLink({ number, className }: { number: number; className?: string }) {
  const href = `https://github.com/${REPO}/pull/${number}`;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`${BRAND_LINK_CLASSES} ${className ?? ''}`}
      data-testid={`pr-link-${number}`}
    >
      PR #{number}
    </a>
  );
}

// ---------------------------------------------------------------------------
// Priority badge — p0 (red), p1 (amber), p2 (stone), p3 (stone muted).
// Amber for p1 is the only amber; per BRAND.md §Design Principles
// "Minimal accent use". See #2805 §2.3.
// ---------------------------------------------------------------------------

const PRIORITY_STYLES: Record<string, string> = {
  // p0/p1 carry the only non-neutral colour weight (red for emergency,
  // amber for "time-sensitive"). p2/p3 collapse to the neutral muted
  // surface — differentiating them with a second shade of amber or
  // stone would cede the BRAND.md "minimal accent use" principle.
  p0: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  p1: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200',
  p2: 'bg-muted text-foreground',
  p3: 'bg-muted text-muted-foreground',
};

export function PriorityBadge({ priority }: { priority: string | null }) {
  if (!priority) {
    return (
      <span className="inline-flex h-5 items-center rounded bg-muted px-1.5 font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
        —
      </span>
    );
  }
  const style = PRIORITY_STYLES[priority] ?? PRIORITY_STYLES.p3;
  return (
    <span
      className={`inline-flex h-5 items-center rounded px-1.5 font-mono text-[10px] font-medium uppercase tracking-wide ${style}`}
      data-testid={`priority-badge-${priority}`}
    >
      {priority}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Outcome glyph pill — succeeded (✓ green), failed (✗ red), crashed (⚠ amber),
// plan_blocked (⊘ neutral — #2857), needs_review (◐ yellow — #2856),
// infra-preempted (↺ amber — #2947).
//
// Colour assignment intentionally keeps red reserved for genuine failure
// and amber reserved for the crash-category anomaly. `plan_blocked`
// ("plan correctly declined to proceed") is operator-informational, so
// it uses the neutral muted surface — the same treatment `priority/p2`
// gets above — per BRAND.md §"Minimal accent use". A dashboard full of
// plan_blocked chips must not look like a house on fire.
//
// `needs_review` is the one correct-outcome terminal that DOES need
// operator action (ralph produced reviewer-approved SHIP code but
// summary flagged unmet AC; the daemon opened a draft PR that sits
// for operator triage). It earns yellow — adjacent to amber on the
// colour wheel but visually distinct from crashed's amber so the
// operator can scan the "Recently completed" panel and quickly
// separate "review my draft PR" from "something crashed, diagnose
// this". The glyph ◐ (half-circle) is the semantic anchor: ralph
// did half the work, you do the other half (review + merge).
//
// `infra_preempted` is a *derived* sub-category of `failed` for the two
// infra-preemption categories in ``scripts/dispatcher/daemon.py``'s
// ``_INFRA_PREEMPTION_CATEGORIES`` frozenset —
// ``daemon_restart_abandoned`` and ``paused_by_killswitch``. These
// agents didn't fail in any code/runtime sense; the dispatcher itself
// interrupted them (restart recovery, or operator hit the killswitch)
// and they will resume on the next tick. Rendering them as red ✗
// misleads operators into treating them as actionable — which is the
// opposite of the truth. They earn ↺ (counterclockwise arrow) in amber
// to signal "will auto-resume; not an operator action item".
//
// Detection is pattern-based on `failureSummary`. The daemon writes
// exactly two canonical strings for these categories —
// ``"dispatcher restarted"`` (daemon_restart_abandoned) and
// ``"manually stopped"`` (paused_by_killswitch) — from
// ``_NO_TAIL_CATEGORY_SUMMARIES`` (issues #2924 / #2935). The resolver
// surfaces these verbatim through ``failureSummary``. Pattern-matching
// on this closed set is robust because the daemon has no other code
// path that can produce these exact strings for a failed row. If the
// daemon ever changes the strings, the pill falls back to the plain
// red ✗ — a soft regression, not a crash.
// ---------------------------------------------------------------------------

/**
 * Canonical failure-summary strings produced by the dispatcher daemon
 * for rows whose `dispatcher.failures.category` is in
 * ``_INFRA_PREEMPTION_CATEGORIES``. Mirrors
 * ``_NO_TAIL_CATEGORY_SUMMARIES`` in ``scripts/dispatcher/daemon.py``.
 *
 * Export for re-use by panels that render their own glyph (e.g. the
 * Recent failures roll-up, which keys off `category` directly and
 * doesn't go through `OutcomePill`). Issue #2947.
 */
export const INFRA_PREEMPTED_SUMMARIES: ReadonlySet<string> = new Set([
  'dispatcher restarted',
  'manually stopped',
]);

/**
 * Canonical `dispatcher.failures.category` values that are
 * infra-preempted (will auto-resume; not operator action items).
 * Mirrors ``_INFRA_PREEMPTION_CATEGORIES`` in
 * ``scripts/dispatcher/daemon.py``. Used by panels (e.g.
 * ``RecentFailuresPanel``) that key off `category` directly rather
 * than matching on `failureSummary`. Issue #2947.
 */
export const INFRA_PREEMPTED_CATEGORIES: ReadonlySet<string> = new Set([
  'daemon_restart_abandoned',
  'paused_by_killswitch',
]);

/** Shared amber chip class for the ↺ infra-preempted glyph. Issue #2947. */
export const INFRA_PREEMPTED_CHIP_CLASSES =
  'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200';

/** Shared ↺ glyph (U+21BA, ANTICLOCKWISE OPEN CIRCLE ARROW). Issue #2947. */
export const INFRA_PREEMPTED_GLYPH = '\u21BA';

type OutcomeStatus =
  | 'succeeded'
  | 'failed'
  | 'crashed'
  | 'plan_blocked'
  | 'needs_review';

const OUTCOME_STYLES: Record<
  OutcomeStatus,
  { glyph: string; className: string; label: string }
> = {
  succeeded: {
    glyph: '\u2713',
    className: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
    label: 'succeeded',
  },
  failed: {
    glyph: '\u2717',
    className: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
    label: 'failed',
  },
  crashed: {
    glyph: '\u26A0',
    className: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200',
    label: 'crashed',
  },
  plan_blocked: {
    // ⊘ = "circled division slash" — signals "declined to proceed"
    // without overlapping the ✗/⚠ iconography reserved for failures.
    glyph: '\u2298',
    className: 'bg-muted text-foreground',
    label: 'plan blocked',
  },
  needs_review: {
    // ◐ = "black circle with left half black" — ralph did half the work
    // (implementation + reviewer SHIP), operator does the other half
    // (review the draft, mark ready, merge). Deliberately distinct from
    // ⚠ (crashed) and ⊘ (plan_blocked) at a glance.
    glyph: '\u25D0',
    // Yellow, not amber — yellow neighbours amber but is perceptibly
    // separate so the two actionable-attention chips don't visually
    // collapse into each other in a scan of the "Recently completed"
    // panel. Uses the `text-yellow-900` foreground for AA contrast
    // against the `bg-yellow-100` surface.
    className: 'bg-yellow-100 text-yellow-900 dark:bg-yellow-900/30 dark:text-yellow-100',
    label: 'needs review',
  },
};

export function OutcomePill({
  status,
  failureSummary,
}: {
  status: string;
  /**
   * Optional one-line "what happened" string. When present (failure
   * terminals: `failed` / `crashed` / `plan_blocked`), overrides the
   * default status-label tooltip so operators can scan the cause on
   * hover without opening the agent detail page. Null / undefined
   * falls back to the default status label — preserves the existing
   * UX for `succeeded` / `needs_review` rows and historical rows from
   * before migration 33. Issue #2900.
   *
   * We deliberately do NOT render an always-visible second line — the
   * "Recently completed" panel's density-first design (#2818) is the
   * constraint. Tooltip-only.
   *
   * Issue #2947: when the summary exactly matches one of
   * ``INFRA_PREEMPTED_SUMMARIES``, the pill re-skins to amber ↺
   * (infra-preempted) instead of red ✗ / amber ⚠ — the agent was
   * preempted by the dispatcher, not an operator action item.
   */
  failureSummary?: string | null;
}) {
  const info = OUTCOME_STYLES[status as OutcomeStatus];
  const trimmedSummary =
    failureSummary && failureSummary.trim() ? failureSummary.trim() : null;
  // #2947: infra-preemption override. Daemon writes a short canonical
  // string (``"dispatcher restarted"`` / ``"manually stopped"``) for
  // the two `_INFRA_PREEMPTION_CATEGORIES` — those rows should render
  // ↺ amber regardless of the stored `status` (today always `failed`
  // for these, but we don't want to couple to that invariant).
  // Pattern-matching on the closed set in ``INFRA_PREEMPTED_SUMMARIES``
  // avoids plumbing a new `category` field through the GraphQL type
  // for what is effectively a display override; any drift in the
  // daemon's canonical strings just collapses back to the default red
  // ✗ (a soft regression, not a crash).
  if (trimmedSummary !== null && INFRA_PREEMPTED_SUMMARIES.has(trimmedSummary)) {
    return (
      <span
        aria-label="infra preempted"
        title={trimmedSummary}
        className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-xs ${INFRA_PREEMPTED_CHIP_CLASSES}`}
        data-testid="outcome-pill-infra_preempted"
      >
        {INFRA_PREEMPTED_GLYPH}
      </span>
    );
  }
  if (!info) {
    return (
      <span
        className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-muted text-[10px] text-muted-foreground"
        title={trimmedSummary ?? undefined}
      >
        ?
      </span>
    );
  }
  // #2900: prefer the failure_summary for the hover tooltip when
  // present. The aria-label stays on the short status label so screen
  // readers announce the category without reading the (possibly long)
  // summary text mid-row. Sighted operators get the full narrative on
  // hover; assistive-tech users hear "failed" / "crashed" and can
  // navigate to the agent detail page from there.
  const tooltip = trimmedSummary ?? info.label;
  return (
    <span
      aria-label={info.label}
      title={tooltip}
      className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-xs ${info.className}`}
      data-testid={`outcome-pill-${status}`}
    >
      {info.glyph}
    </span>
  );
}
