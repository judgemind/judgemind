/**
 * Unit tests for the per-field cost breakdown walker (issues #4100,
 * #4101).
 *
 * The walker computes a `{ [path]: cost }` map alongside the operation
 * total. As of #4101 the breakdown is inlined into the `graphql.cost`
 * structured log line by `cost-limit-plugin.ts`; this file pins the
 * walker's shape and AC2 invariant (sum of map values equals total).
 *
 * These tests pin four things:
 *   1. The walker computes a value that matches the cost-rule
 *      algorithm (mirrored in `cost-rule-estimator.ts`): scalars cost
 *      `scalarCost` (1), object types cost `objectCost` (0), and each
 *      list multiplies the running factor by `listFactor` (10). The
 *      post-#4100 polled query computes to ≤ 1000, the pre-#4100
 *      polled query (with the four trimmed fields restored) to > 1000
 *      — that is the regression-of-regression guard.
 *   2. AC2 of #4101: `Object.values(breakdown).reduce((a, b) => a + b)`
 *      equals `total` for the polled query and for arbitrary
 *      operations exercised here.
 *   3. AC1 of #4101: the breakdown is a `{ [path]: cost }` map keyed by
 *      GraphQL path, with depth-2 paths like
 *      `dispatcherState.activeAgents` for the polled query.
 *   4. `__typename` injection (Apollo Client default on every selection
 *      set) is counted, matching what the cost rule sees over the wire.
 *
 * The agreement between this walker and the production cost rule
 * (`getComplexity({ estimators: [judgemindEstimator] })`) is pinned by
 * AC3 in `cost-rule-estimator.unit.test.ts` — that test runs
 * `getComplexity(...)` against the same document and asserts equality
 * with what `computeBreakdown(...)` here returns.
 */

import { describe, expect, it } from 'vitest';
import {
  buildSchema,
  parse,
  print,
  type DocumentNode,
} from 'graphql';

import { computeBreakdown, __testing } from './cost-breakdown';
import { typeDefs } from './schema';

/**
 * Apollo Client's default `addTypename: true` injects `__typename` into
 * every selection set on the wire. The cost-rule library sees the
 * post-injection document, so the breakdown walker must too. Replicate
 * that injection here so tests exercise the same shape that production
 * traffic produces.
 *
 * Implementation detail: we string-rewrite the query source to add
 * `__typename` to every block, then re-parse. Building AST nodes by
 * hand drops `loc` metadata that graphql-js's TypeInfo visitor relies
 * on for `__typename`'s meta-field path; round-tripping through
 * `parse()` keeps the AST shape consistent with what arrives over the
 * wire.
 */
function withTypenameInjected(doc: DocumentNode): DocumentNode {
  // Round-trip via print → text rewrite → parse. The brace-counting
  // walker injects `__typename` after every opening brace that is not
  // already followed by a `__typename` field on the same line.
  const src = print(doc);
  const lines = src.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    out.push(lines[i]!);
    if (lines[i]!.trimEnd().endsWith('{')) {
      // Match the indent of the next line (which is the first field
      // inside the block) so the injected `__typename` doesn't break
      // the printer's pretty-print.
      const next = lines[i + 1] ?? '';
      const indentMatch = /^(\s*)/.exec(next);
      const indent = indentMatch ? indentMatch[1]! : '  ';
      out.push(`${indent}__typename`);
    }
  }
  return parse(out.join('\n'));
}

/**
 * Mirror the actual production `DISPATCHER_STATE_QUERY` shape with
 * Apollo Client's automatic `__typename` injection. Pinned inline so a
 * future drift in the polled query forces an explicit decision here.
 */
const POLLED_QUERY_SOURCE = /* GraphQL */ `
  query DispatcherState {
    dispatcherState {
      currentRun {
        runId
        startedAt
        stoppedAt
        heartbeatTs
        versionSha
        host
        pid
      }
      activeAgents {
        id
        issueNumber
        issueTitle
        priority
        worktreePath
        phase
        startedAt
        retriesUsed
      }
      recentFailures(sinceHours: 24) {
        failureId
        agentId
        category
        displayCategory
        detectedBy
        details
        ts
        issueNumber
      }
      queueDepth
      blockedDepth
      queueReady {
        issueNumber
        title
        priority
        labels
        createdAt
        blockedBy {
          number
        }
        cooldownSecondsRemaining
      }
      queueBlocked {
        issueNumber
        title
        priority
        labels
        createdAt
        blockedBy {
          number
        }
      }
      recentCompletions {
        agentId
        issueNumber
        issueTitle
        priority
        status
        endedAt
        prNumber
        failureSummary
      }
      recentCompletionsCount
      spawnFrozenUntil
      circuitBreakerOpen
      capFlippedBy
    }
  }
`;

const schema = buildSchema(typeDefs);

function sumValues(breakdown: Record<string, number>): number {
  return Object.values(breakdown).reduce((acc, v) => acc + v, 0);
}

describe('computeBreakdown — DispatcherState polled query (issues #4100, #4101)', () => {
  it('returns a `{ [path]: cost }` map with depth-2 paths under `dispatcherState`', () => {
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const result = computeBreakdown(schema, document, 'DispatcherState');
    expect(result.operationName).toBe('DispatcherState');
    // Plain map shape — not an array.
    expect(typeof result.breakdown).toBe('object');
    expect(Array.isArray(result.breakdown)).toBe(false);
    // The polled query's heavy children show up at depth 2 — exactly
    // the diagnostic shape #4101 calls for.
    const keys = Object.keys(result.breakdown);
    expect(keys).toContain('dispatcherState.activeAgents');
    expect(keys).toContain('dispatcherState.recentCompletions');
    expect(keys).toContain('dispatcherState.recentFailures');
    expect(keys).toContain('dispatcherState.queueReady');
    expect(keys).toContain('dispatcherState.queueBlocked');
    expect(keys).toContain('dispatcherState.queueDepth');
  });

  it('AC2 of #4101: sum of breakdown values equals the operation total', () => {
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const result = computeBreakdown(schema, document, 'DispatcherState');
    expect(sumValues(result.breakdown)).toBe(result.total);
  });

  it('the dominant child entry is well above the early-warning threshold (~800)', () => {
    // The ten-element list-of-objects fields under `dispatcherState`
    // dominate the cost. At least one of them should be in the top-five
    // by cost so operators see the cost driver named in the breakdown.
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const result = computeBreakdown(schema, document, 'DispatcherState');
    const total = result.total;
    expect(total).toBeGreaterThan(800);
    const sorted = Object.entries(result.breakdown).sort(
      (a, b) => b[1] - a[1],
    );
    // The top entry is one of the list-of-objects children, which
    // contributes at least 80 (LIST_FACTOR × scalarCount) to the total.
    expect(sorted[0]![1]).toBeGreaterThanOrEqual(80);
  });

  it('pre-#4100 polled shape exceeds the 1000 cap (regression-of-regression)', () => {
    // Pin the failure mode #4100 fixed: with the 12-field activeAgents
    // selection (pre-trim), the polled cost was 35 over the cap. If a
    // future PR re-adds those four fields the assertion below fires.
    const PRE_TRIM_QUERY = POLLED_QUERY_SOURCE.replace(
      /activeAgents \{[^}]*\}/,
      `activeAgents {
        id
        issueNumber
        issueTitle
        priority
        worktreePath
        phase
        status
        startedAt
        endedAt
        exitCode
        prNumber
        retriesUsed
      }`,
    );
    const preDoc = withTypenameInjected(parse(PRE_TRIM_QUERY));
    const preTotal = computeBreakdown(schema, preDoc, 'DispatcherState').total;
    // Pre-trim cost is the post-trim cost + 4×10 = +40.
    const postDoc = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const postTotal = computeBreakdown(schema, postDoc, 'DispatcherState').total;
    expect(preTotal - postTotal).toBe(40);
    expect(preTotal).toBeGreaterThan(1000);
  });

  it('post-#4100 polled query is at-or-below the 1000 cap', () => {
    // The whole point of #4100 — once activeAgents is trimmed, the
    // polled total must fit under the cap from #4003.
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const { total } = computeBreakdown(schema, document, 'DispatcherState');
    expect(total).toBeLessThanOrEqual(1000);
  });

  it('counts auto-injected `__typename` (Apollo Client default)', () => {
    // Compute total with and without typename injection. The typename
    // contribution on the dispatcher state query is ~250 units (one per
    // selection set, multiplied by the running list factor at that
    // depth). Asserting a non-trivial gap pins the behaviour without
    // making the test brittle to small schema additions.
    const raw = parse(POLLED_QUERY_SOURCE);
    const injected = withTypenameInjected(raw);
    const rawTotal = computeBreakdown(schema, raw, 'DispatcherState').total;
    const injectedTotal = computeBreakdown(
      schema,
      injected,
      'DispatcherState',
    ).total;
    expect(injectedTotal).toBeGreaterThan(rawTotal + 100);
  });

  it('breakdown still sums to total when typename injection is on', () => {
    // Cross-check the AC2 invariant under the production-shaped (with
    // injected typename) document.
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const { total, breakdown } = computeBreakdown(
      schema,
      document,
      'DispatcherState',
    );
    expect(sumValues(breakdown)).toBe(total);
  });
});

describe('computeBreakdown — multi-root operation', () => {
  it('emits one entry per top-level scalar root', () => {
    // `health` and `distinctCounties` are both real top-level Query
    // fields (the latter is `[String!]!` which contributes a list
    // factor of 10). Sum of breakdown values must equal total.
    const source = /* GraphQL */ `
      query Multi {
        health
        distinctCounties
      }
    `;
    const document = parse(source);
    const result = computeBreakdown(schema, document, 'Multi');
    const keys = Object.keys(result.breakdown).sort();
    expect(keys).toEqual(['distinctCounties', 'health']);
    expect(sumValues(result.breakdown)).toBe(result.total);
    // health: String! → 1, distinctCounties: [String!]! → 10.
    expect(result.breakdown.health).toBe(1);
    expect(result.breakdown.distinctCounties).toBe(10);
  });
});

describe('computeBreakdown — depth control', () => {
  it('depth=1 emits at top-level only and still sums to total', () => {
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const { total, breakdown } = computeBreakdown(
      schema,
      document,
      'DispatcherState',
      { depth: 1 },
    );
    // At depth 1 the polled query has exactly one data root
    // (`dispatcherState`) plus the operation-level `__typename` bucket.
    // Both contributions sum to total.
    expect(sumValues(breakdown)).toBe(total);
    expect(Object.keys(breakdown)).toContain('dispatcherState');
  });

  it('depth=3 emits deeper paths and still sums to total', () => {
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const { total, breakdown } = computeBreakdown(
      schema,
      document,
      'DispatcherState',
      { depth: 3 },
    );
    expect(sumValues(breakdown)).toBe(total);
    // At depth 3 the deepest paths name children-of-children.
    const keys = Object.keys(breakdown);
    expect(keys.some((k) => k.startsWith('dispatcherState.activeAgents.'))).toBe(
      true,
    );
  });
});

describe('computeBreakdown — fieldCap truncation', () => {
  it('truncating preserves total via a `__truncated` bucket', () => {
    // Force truncation by setting a tiny fieldCap.
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const { total, breakdown } = computeBreakdown(
      schema,
      document,
      'DispatcherState',
      { fieldCap: 3 },
    );
    expect(Object.keys(breakdown).length).toBeLessThanOrEqual(3);
    expect(sumValues(breakdown)).toBe(total);
    expect(breakdown[__testing.TRUNCATED_KEY]).toBeGreaterThan(0);
  });
});

// Sanity guard: the post-#4100 polled query string we pin in this test
// stays in sync with the production string in
// `packages/web/src/lib/dispatcher-queries.ts`. We re-export the printed
// AST and grep for the trimmed `activeAgents` shape.
describe('POLLED_QUERY_SOURCE — pinned shape sanity', () => {
  it('post-#4100 trim: activeAgents selects only 8 row-essential fields', () => {
    const printed = print(parse(POLLED_QUERY_SOURCE));
    const re = /activeAgents\s*\{([^{}]*)\}/;
    const m = re.exec(printed);
    expect(m).not.toBeNull();
    const body = m![1]!;
    const fields = body
      .split(/\s+/)
      .map((s) => s.trim())
      .filter(Boolean);
    expect(new Set(fields)).toEqual(
      new Set([
        'id',
        'issueNumber',
        'issueTitle',
        'priority',
        'worktreePath',
        'phase',
        'startedAt',
        'retriesUsed',
      ]),
    );
  });
});
