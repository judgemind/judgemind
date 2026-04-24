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
 *
 * §2.3 Amber/yellow audit pass (Phase 2, #2812):
 *   - `BRAND_LINK_CLASSES` uses amber — hot links, explicitly exempt.
 *   - `p1` PriorityBadge uses amber — time-sensitive tier, explicitly exempt.
 *   - `INCOMPLETE_SUCCESS_CLASSES` uses amber — shipped-but-incomplete signal,
 *     semantically distinct from p1 amber (different chip size/shape/context).
 *   - `crashed` OutcomePill uses amber — pipeline anomaly requiring operator
 *     attention; different shape (circular) from the hot-link and priority badge.
 *   - `needs_review` OutcomePill uses yellow (not amber) — deliberately
 *     adjacent but perceptibly distinct, per §2.3 comment.
 *   - All other amber/yellow usages are semantic and documented. No spurious
 *     decorative amber was introduced. Review confirmed: BRAND.md §3
 *     "Minimal accent use" principle is satisfied.
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
// Outcome glyph pill — the single visual summary of an agent's terminal
// state. One glyph per row; colour encodes pipeline completeness.
//
// Colour vocabulary (issue #2953):
// * green  ✓ — shipped + post-merge bookkeeping complete (merged +
//              verified OR verify-skipped-with-reason + retroed)
// * amber  ✓ — shipped but post-merge bookkeeping incomplete (merged
//              but missing verifiedAt AND no skip reason, OR missing
//              retroedAt). Operator signal: "this PR landed, but
//              verify/retro didn't finish — investigate why before
//              trusting the shipment."
// * red    ✗ — genuine failure (didn't merge).
// * gray   ↺ — infra-preempted (dispatcher restart / killswitch). Not
//              an operator action item; will auto-resume. Issue #2953
//              changes the colour from amber (#2947) to gray to sit
//              the glyph on the neutral palette — infra churn is not
//              a warning, it's routine resumption.
// * yellow ◐ — needs_review (#2856): ralph produced reviewer-approved
//              SHIP code but summary flagged unmet AC; daemon opened a
//              draft PR for operator triage.
// * neutral⊘ — plan_blocked (#2857): plan correctly declined to
//              proceed. Operator-informational, not alarming.
// * fallback? — unknown status.
//
// Tooltip:
// For a merged row, the tooltip renders a milestone breakdown —
// `merged 22:35 · verified 22:41 · retro 22:50` or
// `merged 22:35 · verify skipped (self-deploy) · retro 22:48` or
// `merged 22:35 · verified — · retro — (container restarted before retry)` —
// so the operator can at-a-glance see where the pipeline stopped.
// Non-merged rows keep the pre-#2953 tooltip behaviour (failureSummary
// or status-label fallback).
//
// `infra_preempted` is detected from `failureSummary` matching one of
// the canonical strings produced by the daemon's
// `_NO_TAIL_CATEGORY_SUMMARIES` map — `"dispatcher restarted"` (from
// `daemon_restart_abandoned`) and `"manually stopped"` (from
// `paused_by_killswitch`). This avoids plumbing a new `category`
// field through GraphQL for what is effectively a display override;
// any drift in the daemon strings just collapses back to the default
// chip (soft regression, not a crash).
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

/** Shared gray chip class for the ↺ infra-preempted glyph.
 *
 * Issue #2953: recoloured from amber (#2947) to gray. An infra-
 * preempt is not a warning — it's a neutral resumption event
 * (dispatcher restarted, operator hit killswitch, etc.). Amber
 * inappropriately visually groups it with the amber ✓ "shipped but
 * bookkeeping incomplete" state introduced by this issue; gray keeps
 * the two clearly distinct. Uses `bg-muted` / `text-muted-foreground`
 * so the chip inherits the shadcn neutral palette — same treatment as
 * `plan_blocked` (⊘), with no accent weight. */
export const INFRA_PREEMPTED_CHIP_CLASSES =
  'bg-muted text-muted-foreground';

/** Shared ↺ glyph (U+21BA, ANTICLOCKWISE OPEN CIRCLE ARROW). Issue #2947. */
export const INFRA_PREEMPTED_GLYPH = '\u21BA';

type OutcomeStatus =
  | 'succeeded'
  | 'failed'
  | 'crashed'
  | 'plan_blocked'
  | 'needs_review';

/** Amber ✓ chip — shipped but post-merge bookkeeping incomplete.
 *
 * Issue #2953. Written when a row is ``status='succeeded'`` with a
 * ``mergedAt`` timestamp (so it genuinely shipped) but either
 * ``verifiedAt`` is missing with no skip reason, or ``retroedAt``
 * is missing. The amber colour is distinct from the green ✓
 * (``bg-green-100``), yellow ◐ (``bg-yellow-100``), and gray ↺
 * (``bg-muted``) so the operator can scan the panel and separate
 * the four "shipped" sub-states at a glance. */
const INCOMPLETE_SUCCESS_CLASSES =
  'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200';

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

/** Format a milestone timestamp for the tooltip. Returns `HH:MM` in
 * the viewer's local time (no date — the panel already scopes to
 * recently-completed rows, full datetime is on the relative-time
 * hover). Returns an em-dash for null / undefined / unparseable
 * inputs so the tooltip format is stable. Issue #2953. */
function formatMilestoneTime(ts: string | null | undefined): string {
  if (!ts) return '\u2014';
  const parsed = Date.parse(ts);
  if (!Number.isFinite(parsed)) return '\u2014';
  const d = new Date(parsed);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

/** Render the milestone-breakdown tooltip for a row. Issue #2953.
 *
 * Format:
 *   `merged HH:MM · verified HH:MM · retro HH:MM`
 *   `merged HH:MM · verify skipped (<reason>) · retro HH:MM`
 *   `merged HH:MM · verified — · retro — (post-merge bookkeeping incomplete)`
 *
 * Rendered inside the native `title` attribute — plain text, no HTML.
 * Reuses the same em-dash placeholder the milestone-time formatter
 * produces so the tooltip shape is stable regardless of which
 * milestones landed. */
export function formatMilestoneTooltip(args: {
  mergedAt: string | null;
  verifiedAt: string | null;
  verifySkipReason: string | null;
  retroedAt: string | null;
}): string {
  const parts: string[] = [];
  parts.push(`merged ${formatMilestoneTime(args.mergedAt)}`);
  if (args.verifySkipReason) {
    // Render the skip reason with underscores → spaces so
    // ``self_deploy`` reads as ``self-deploy``. Keep the parens so
    // the tooltip is scannable.
    const pretty = args.verifySkipReason.replace(/_/g, '-');
    parts.push(`verify skipped (${pretty})`);
  } else {
    parts.push(`verified ${formatMilestoneTime(args.verifiedAt)}`);
  }
  parts.push(`retro ${formatMilestoneTime(args.retroedAt)}`);

  // If the row merged but any post-merge milestone is missing with no
  // skip reason, append a short trailing hint so the operator can
  // spot the incomplete-bookkeeping case at a glance.
  const incomplete =
    !!args.mergedAt &&
    ((!args.verifiedAt && !args.verifySkipReason) || !args.retroedAt);
  if (incomplete) {
    parts.push('post-merge bookkeeping incomplete');
  }
  return parts.join(' \u00B7 ');
}

/** Return the pipeline-completeness category for a row. Issue #2953.
 *
 * - ``fully_complete`` — merged + (verified OR skipped with reason) +
 *   retroed.
 * - ``shipped_incomplete`` — merged, but verify and/or retro bookkeeping
 *   did not complete (no skip reason to explain verify, or retro is
 *   missing).
 * - ``not_merged`` — no ``mergedAt`` signal; the base ``status`` drives
 *   the glyph (failed / crashed / plan_blocked / needs_review).
 *
 * Pure-functional for testability. */
export function classifyPipelineCompleteness(args: {
  mergedAt: string | null;
  verifiedAt: string | null;
  verifySkipReason: string | null;
  retroedAt: string | null;
}): 'fully_complete' | 'shipped_incomplete' | 'not_merged' {
  if (!args.mergedAt) return 'not_merged';
  const verifyDone = !!args.verifiedAt || !!args.verifySkipReason;
  const retroDone = !!args.retroedAt;
  return verifyDone && retroDone ? 'fully_complete' : 'shipped_incomplete';
}

export function OutcomePill({
  status,
  failureSummary,
  mergedAt = null,
  verifiedAt = null,
  verifySkipReason = null,
  retroedAt = null,
}: {
  status: string;
  /**
   * Optional one-line "what happened" string. When present (failure
   * terminals: `failed` / `crashed` / `plan_blocked`), overrides the
   * default status-label tooltip so operators can scan the cause on
   * hover without opening the agent detail page. Null / undefined
   * falls back to the default status label. Issue #2900.
   *
   * Issue #2947: when the summary exactly matches one of
   * ``INFRA_PREEMPTED_SUMMARIES``, the pill re-skins to ↺ gray
   * (infra-preempted) instead of red ✗ / amber ⚠. Issue #2953 moves
   * the infra-preempt colour from amber → gray — see
   * ``INFRA_PREEMPTED_CHIP_CLASSES``.
   */
  failureSummary?: string | null;
  /** Issue #2953 — PR-merge timestamp. When present the pill uses the
   * milestone-completeness rules (green ✓ vs. amber ✓) instead of the
   * base status mapping. When null the pill falls back to the pre-
   * #2953 status-only path. */
  mergedAt?: string | null;
  /** Issue #2953 — verify phase timestamp. */
  verifiedAt?: string | null;
  /** Issue #2953 — ``dispatcher.agents.verify_skip_reason``. Non-null
   * means verify was intentionally skipped (today: ``self_deploy``).
   * A merged row with a skip reason counts as fully-verified for the
   * glyph colour — the skip was intentional, not a regression. */
  verifySkipReason?: string | null;
  /** Issue #2953 — retro phase timestamp. */
  retroedAt?: string | null;
}) {
  const info = OUTCOME_STYLES[status as OutcomeStatus];
  const trimmedSummary =
    failureSummary && failureSummary.trim() ? failureSummary.trim() : null;

  // #2947 / #2953: infra-preemption override. Daemon writes a short
  // canonical string (``"dispatcher restarted"`` / ``"manually
  // stopped"``) for the two `_INFRA_PREEMPTION_CATEGORIES` — those
  // rows render ↺ gray (was amber in #2947; #2953 recoloured to gray
  // to keep infra churn on the neutral palette, distinct from the
  // amber ✓ "shipped but bookkeeping incomplete" chip introduced by
  // #2953). Pattern-matching on the closed set in
  // ``INFRA_PREEMPTED_SUMMARIES`` avoids plumbing a new `category`
  // field through GraphQL; any drift in the daemon strings just
  // collapses back to the default red ✗ (soft regression, not a
  // crash).
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

  // #2953: milestone-completeness path. When the row has a
  // ``mergedAt`` stamp, it genuinely shipped — the glyph is always ✓,
  // and the colour encodes whether the post-merge bookkeeping (verify
  // + retro) completed. The milestone-breakdown tooltip replaces the
  // status-label / failure-summary tooltips for merged rows so the
  // operator can scan "where did this land in the pipeline" on hover
  // without opening the agent detail page.
  //
  // Only applied when ``status`` resolves to 'succeeded' OR the row
  // has a mergedAt (defensive — a row flipped back to 'failed' after
  // a post-merge verify failure stays in the failure path, but we
  // still want the tooltip to surface that it did merge).
  if (mergedAt) {
    const completeness = classifyPipelineCompleteness({
      mergedAt,
      verifiedAt,
      verifySkipReason,
      retroedAt,
    });
    const milestoneTooltip = formatMilestoneTooltip({
      mergedAt,
      verifiedAt,
      verifySkipReason,
      retroedAt,
    });

    // Genuine post-merge failure (verify FAILED → status flipped to
    // 'failed' by _run_verify_and_complete). We keep the red ✗ glyph
    // but the tooltip surfaces the milestone breakdown so the
    // operator sees that the PR *did* ship before verify rejected it.
    if (status === 'failed' || status === 'crashed') {
      const failureInfo = info ?? OUTCOME_STYLES.failed;
      const combinedTooltip = trimmedSummary
        ? `${trimmedSummary} — ${milestoneTooltip}`
        : milestoneTooltip;
      return (
        <span
          aria-label={failureInfo.label}
          title={combinedTooltip}
          className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-xs ${failureInfo.className}`}
          data-testid={`outcome-pill-${status}`}
        >
          {failureInfo.glyph}
        </span>
      );
    }

    // The succeeded / merged branch — green ✓ when fully complete,
    // amber ✓ when shipped but bookkeeping incomplete.
    const className =
      completeness === 'fully_complete'
        ? OUTCOME_STYLES.succeeded.className
        : INCOMPLETE_SUCCESS_CLASSES;
    const label =
      completeness === 'fully_complete'
        ? 'succeeded'
        : 'shipped, post-merge bookkeeping incomplete';
    const testId =
      completeness === 'fully_complete'
        ? 'outcome-pill-succeeded'
        : 'outcome-pill-succeeded-incomplete';
    return (
      <span
        aria-label={label}
        title={milestoneTooltip}
        className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-xs ${className}`}
        data-testid={testId}
      >
        {OUTCOME_STYLES.succeeded.glyph}
      </span>
    );
  }

  // No mergedAt — fall back to the pre-#2953 status-only path.
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
