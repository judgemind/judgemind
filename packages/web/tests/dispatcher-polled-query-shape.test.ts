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
