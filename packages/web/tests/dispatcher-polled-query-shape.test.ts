import { describe, it, expect } from 'vitest';
import { print } from 'graphql';
import {
  DISPATCHER_STATE_QUERY,
  DISPATCHER_QUEUE_FULL_QUERY,
} from '../src/lib/dispatcher-queries';

/**
 * Regression test for issue #4062.
 *
 * The polled `DISPATCHER_STATE_QUERY` is fired every 2s by the cockpit
 * dashboard. The #4003 GraphQL DoS hardening introduced a per-query
 * complexity cap of 1000, which the dashboard query was exceeding
 * (cost: 1355). The biggest single contributor to that cost was the
 * nested `blockedBy { number title }` selection inside the two queue
 * lists — `graphql-validation-complexity` multiplies cost for each list
 * nesting level, and list-of-objects-containing-a-list is the most
 * expensive shape.
 *
 * The fix in this PR drops `blockedBy.title` from the polled query and
 * keeps only `blockedBy.number`. Blocker titles remain on the
 * expand-on-click `DISPATCHER_QUEUE_FULL_QUERY`, which fires only on
 * dialog open (not in the poll path).
 *
 * If a future change re-adds `title` inside the polled query's
 * `blockedBy` selections, this test fails and forces an explicit
 * decision about the cost regression.
 */

/**
 * Extract every selection-set body that follows the regex pattern
 * `<fieldName> { ... }` in the printed query, with the `{ ... }` matched
 * non-greedily on a per-block basis.
 *
 * Returns the contents of every `<fieldName> { ... }` block, in source
 * order. Nested braces inside the field block (e.g. another nested
 * selection) are not expected for the leaf-list shapes we inspect here
 * (`blockedBy` in the polled query has only scalar fields), so a simple
 * non-greedy match is sufficient.
 */
function selectionBodiesFor(query: string, fieldName: string): string[] {
  const re = new RegExp(`${fieldName}\\s*\\{([^{}]*)\\}`, 'g');
  const out: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(query)) !== null) {
    out.push(m[1]);
  }
  return out;
}

describe('DISPATCHER_STATE_QUERY — polled query shape (issue #4062)', () => {
  const printed = print(DISPATCHER_STATE_QUERY);

  it('is the expected operation', () => {
    expect(printed).toMatch(/query\s+DispatcherState\b/);
  });

  it('selects blockedBy in both queueReady and queueBlocked', () => {
    // Sanity: we expect exactly two `blockedBy { ... }` blocks in the
    // polled query — one inside queueReady, one inside queueBlocked.
    const bodies = selectionBodiesFor(printed, 'blockedBy');
    expect(bodies).toHaveLength(2);
  });

  it('omits `title` from every `blockedBy` selection in the polled query', () => {
    // AC5 (regression): a frontend-side test asserts the polled query
    // string does not contain `title` inside the two `blockedBy`
    // selections. The polled query only renders `#N` count info; full
    // titles are populated via DISPATCHER_QUEUE_FULL_QUERY.
    const bodies = selectionBodiesFor(printed, 'blockedBy');
    for (const body of bodies) {
      expect(body).toMatch(/\bnumber\b/);
      expect(body).not.toMatch(/\btitle\b/);
    }
  });
});

describe('DISPATCHER_QUEUE_FULL_QUERY — dialog query still selects title', () => {
  const printed = print(DISPATCHER_QUEUE_FULL_QUERY);

  it('keeps `title` inside the dialog query `blockedBy` selection', () => {
    // AC4 (regression guard the other way): the expand-on-click dialog
    // continues to surface blocker titles. If a future cleanup also
    // strips `title` from this query, the dialog tooltip silently loses
    // information — fail loudly here instead.
    const bodies = selectionBodiesFor(printed, 'blockedBy');
    expect(bodies.length).toBeGreaterThan(0);
    for (const body of bodies) {
      expect(body).toMatch(/\btitle\b/);
      expect(body).toMatch(/\bnumber\b/);
    }
  });
});

/**
 * Regression test for issue #4064.
 *
 * The polled `DISPATCHER_STATE_QUERY.recentCompletions` block originally
 * selected 14 fields. Combined with the 10× list-of-objects multiplier
 * applied by `graphql-validation-complexity`, this contributed the
 * third-largest chunk of the 1355-unit query cost (cap is 1000).
 *
 * The fix in this PR trims the polled selection to the row-essential
 * fields only — the eight needed by `RecentCompletionRow` to render
 * the visible row layout (outcome pill, issue link, priority badge,
 * PR link, title, "Nh ago" relative time). Detail-only fields
 * (`startedAt` for the Start/Duration tooltip, the per-phase token /
 * cost footnote, and the milestone columns that drive the green vs.
 * amber ✓ pill nuance) are dropped from the polled path and continue
 * to ride on the existing `DISPATCHER_QUEUE_FULL_QUERY` (fired only
 * when the operator opens the expand-on-click dialog).
 *
 * If a future change re-adds any of the dropped fields inside the
 * polled query's `recentCompletions` selection, this test fails and
 * forces an explicit decision about the cost regression.
 */
describe('DISPATCHER_STATE_QUERY — recentCompletions polled shape (issue #4064)', () => {
  const printed = print(DISPATCHER_STATE_QUERY);

  it('selects exactly one `recentCompletions { ... }` block', () => {
    const bodies = selectionBodiesFor(printed, 'recentCompletions');
    expect(bodies).toHaveLength(1);
  });

  it('keeps row-essential fields in the polled `recentCompletions` selection', () => {
    // The eight fields the visible row in `RecentCompletionRow` reads
    // directly: agent identity (key), unified `(issue, priority, [PR,]
    // title)` prefix, the OutcomePill `status` driver, the relative-time
    // cell's `endedAt`, and the `failureSummary` that re-skins the pill
    // to gray ↺ for infra-preempted rows (`dispatcher restarted` /
    // `manually stopped`).
    const bodies = selectionBodiesFor(printed, 'recentCompletions');
    expect(bodies).toHaveLength(1);
    const body = bodies[0];
    for (const field of [
      'agentId',
      'issueNumber',
      'issueTitle',
      'priority',
      'status',
      'endedAt',
      'prNumber',
      'failureSummary',
    ]) {
      expect(body).toMatch(new RegExp(`\\b${field}\\b`));
    }
  });

  it('omits detail-only fields from the polled `recentCompletions` selection', () => {
    // These are surfaced via `DISPATCHER_QUEUE_FULL_QUERY` when the
    // operator opens the expand-on-click dialog. Keeping them out of
    // the 2s-polled query is what brings the polled cost back under
    // the #4003 1000-cap.
    //
    // - `startedAt` → tooltip-only (Start/Duration/End hover); the
    //   visible "Nh ago" cell is computed from `endedAt` alone.
    // - `totalTokens` / `totalCostUsd` → cost footnote on the row;
    //   absent from polled so the footnote silently drops, restored
    //   on dialog open.
    // - `mergedAt` / `verifiedAt` / `verifySkipReason` / `retroedAt`
    //   → milestone tooltip + the green-vs-amber-✓ "shipped but
    //   bookkeeping incomplete" colour nuance. Polled rows fall back
    //   to the base status pill (green ✓ for succeeded, etc.); the
    //   full milestone breakdown still renders in the dialog row.
    const bodies = selectionBodiesFor(printed, 'recentCompletions');
    expect(bodies).toHaveLength(1);
    const body = bodies[0];
    for (const field of [
      'startedAt',
      'totalTokens',
      'totalCostUsd',
      'mergedAt',
      'verifiedAt',
      'verifySkipReason',
      'retroedAt',
    ]) {
      expect(body).not.toMatch(new RegExp(`\\b${field}\\b`));
    }
  });
});

describe('DISPATCHER_QUEUE_FULL_QUERY — dialog completions still rich (issue #4064)', () => {
  const printed = print(DISPATCHER_QUEUE_FULL_QUERY);

  it('keeps detail fields inside the dialog `completions` selection', () => {
    // AC3 regression guard: the expand-on-click dialog is the place
    // detail fields live now. If a future cleanup also strips them
    // from this query, the dialog row silently loses the cost
    // footnote, milestone tooltip, and start/duration tooltip — fail
    // loudly here instead.
    const bodies = selectionBodiesFor(printed, 'completions');
    expect(bodies.length).toBeGreaterThan(0);
    for (const body of bodies) {
      for (const field of [
        'totalTokens',
        'totalCostUsd',
        'failureSummary',
        'mergedAt',
        'verifiedAt',
        'verifySkipReason',
        'retroedAt',
      ]) {
        expect(body).toMatch(new RegExp(`\\b${field}\\b`));
      }
    }
  });
});

/**
 * Regression test for issue #4100.
 *
 * After #4062 + #4063 + #4064 landed, the polled `DISPATCHER_STATE_QUERY`
 * was still 35 units over the #4003 1000-cap (cost: 1035) — every cockpit
 * poll continued to fail with HTTP 400 `Query exceeded complexity`. The
 * next-largest contributor by inspection was `activeAgents`, a 12-field
 * scalar list multiplied by 10× for being a list-of-objects.
 *
 * The fix in this PR trims `activeAgents` to the eight fields
 * `ActiveAgentRow` actually reads. Detail-only fields that the row never
 * renders (`status` is implicit "running" by definition, `endedAt` /
 * `exitCode` are terminal-only, `prNumber` has no row affordance) are
 * dropped from the polled path and continue to ride on
 * `DISPATCHER_QUEUE_FULL_QUERY`.
 *
 * If a future change re-adds any of the dropped fields inside the
 * polled query's `activeAgents` selection, this test fails and forces
 * an explicit decision about the cost regression.
 */
describe('DISPATCHER_STATE_QUERY — activeAgents polled shape (issue #4100)', () => {
  const printed = print(DISPATCHER_STATE_QUERY);

  it('selects exactly one `activeAgents { ... }` block', () => {
    const bodies = selectionBodiesFor(printed, 'activeAgents');
    expect(bodies).toHaveLength(1);
  });

  it('keeps the eight row-essential fields in the polled `activeAgents` selection', () => {
    // The eight fields `ActiveAgentRow` reads directly to render:
    //   - `id`           — React key + native title for full UUID
    //   - `worktreePath` — Logs link href and path-tail fallback
    //   - `phase`        — phase chip text + tooltip via
    //                      `dispatcher-phase-flow`
    //   - `issueNumber`, `issueTitle`, `priority` — the unified
    //     `(issue, priority, title)` prefix shared with QueueRow and
    //     RecentCompletionRow (#2899 / #3583)
    //   - `startedAt`    — elapsed-time cell (`formatUptime`)
    //   - `retriesUsed`  — final-attempt Opus marker
    //                      (`isFinalRalphAttempt`)
    const bodies = selectionBodiesFor(printed, 'activeAgents');
    expect(bodies).toHaveLength(1);
    const body = bodies[0];
    for (const field of [
      'id',
      'issueNumber',
      'issueTitle',
      'priority',
      'worktreePath',
      'phase',
      'startedAt',
      'retriesUsed',
    ]) {
      expect(body).toMatch(new RegExp(`\\b${field}\\b`));
    }
  });

  it('omits detail-only fields from the polled `activeAgents` selection', () => {
    // - `status`    — by SQL definition every `activeAgents` row has
    //                 status='running'; the polled view doesn't need
    //                 to read it.
    // - `endedAt` / `exitCode` — terminal-only signals; always null on
    //                 a still-running row, so carrying them costs 20
    //                 units of complexity for zero render information.
    // - `prNumber`  — rare on running rows (only present after summary
    //                 has opened a PR) and `ActiveAgentRow` has no UI
    //                 affordance for the link. Restored on dialog open.
    const bodies = selectionBodiesFor(printed, 'activeAgents');
    expect(bodies).toHaveLength(1);
    const body = bodies[0];
    for (const field of ['status', 'endedAt', 'exitCode', 'prNumber']) {
      expect(body).not.toMatch(new RegExp(`\\b${field}\\b`));
    }
  });
});
