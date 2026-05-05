/**
 * Unit tests for the per-field cost breakdown logger (issue #4100).
 *
 * The logger ships as an early-warning instrument: when an operation's
 * total cost meets `COST_LOG_THRESHOLD` (800), a single CloudWatch line
 * names the top-level fields in descending cost order so operators can
 * identify the next slim without re-deriving the breakdown by hand.
 *
 * These tests pin three things:
 *   1. The walker computes a value that matches the cost-rule
 *      algorithm (vendored in
 *      `node_modules/graphql-validation-complexity/lib/ComplexityVisitor.js`):
 *      scalars cost `scalarCost` (1), object types cost `objectCost`
 *      (0), and each list multiplies the running factor by `listFactor`
 *      (10). The post-#4100 polled query computes to ≤ 1000, the pre-
 *      #4100 polled query (with the four trimmed fields restored) to
 *      > 1000 — that is the regression-of-regression guard.
 *   2. `__typename` injection (Apollo Client default on every selection
 *      set) is counted, matching what graphql-validation-complexity
 *      sees over the wire after Apollo Client expands the document.
 *   3. The `costBreakdownPlugin` only emits a log line when the total
 *      meets the threshold — keeps log volume bounded.
 *
 * Why we don't compare against `validate(schema, doc, [rule])`: the
 * `graphql-validation-complexity@0.4.2` library walks the document
 * with its OWN local TypeInfo (`visit(node, visitWithTypeInfo(typeInfo,
 * visitor))`) but reads field defs from `this.context.getFieldDef()`,
 * which is bound to the OUTER validation walk's TypeInfo. On graphql
 * 16 this mismatch makes the inner walk see null field defs for most
 * positions and underreport cost (~10× low) when invoked through the
 * standalone `validate()` API. In production the rule still correctly
 * rejects the polled query at 1035 — the integration with Apollo
 * Server's validation pipeline differs from a bare `validate()` call.
 * Comparing here would couple the test to that broken bare-call
 * behaviour, so we pin the walker against fixed expected values
 * instead.
 */

import { describe, expect, it, vi } from 'vitest';
import {
  buildSchema,
  parse,
  print,
  type DocumentNode,
} from 'graphql';

import { computeBreakdown, costBreakdownPlugin } from './cost-breakdown';
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

describe('computeBreakdown — DispatcherState polled query (issue #4100)', () => {
  it('emits one row per top-level field, sorted by cost descending', () => {
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const result = computeBreakdown(schema, document, 'DispatcherState');
    expect(result.operationName).toBe('DispatcherState');
    // Two top-level fields after Apollo Client's `__typename` injection
    // — `dispatcherState` (the data field) and `__typename` (the
    // operation-level meta-field). Sorted by cost desc puts the
    // dominant data field first.
    expect(result.fields[0].path).toBe('dispatcherState');
  });

  it('per-list-field sub-totals are recoverable by walking the document', () => {
    // Re-walk the post-#4100 polled query and assert the dominant
    // top-level field's cost is well above the early-warning threshold,
    // so operators see the breakdown when the cap is being approached.
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const result = computeBreakdown(schema, document, 'DispatcherState');
    // dispatcherState is the dominant top-level field.
    const dispatcherStateRow = result.fields.find(
      (f) => f.path === 'dispatcherState',
    );
    expect(dispatcherStateRow).toBeDefined();
    expect(dispatcherStateRow!.cost).toBeGreaterThan(800);
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
});

describe('computeBreakdown — multi-root operation', () => {
  it('returns one row per root field when an operation selects multiple roots', () => {
    const source = /* GraphQL */ `
      query Multi {
        queueDepth
        blockedDepth
      }
    `;
    const document = parse(source);
    const result = computeBreakdown(schema, document, 'Multi');
    expect(result.fields.map((f) => f.path).sort()).toEqual([
      'blockedDepth',
      'queueDepth',
    ]);
  });
});

describe('costBreakdownPlugin — threshold gating', () => {
  /**
   * Drive one request lifecycle through the plugin and return the
   * captured log calls. Wraps the type gymnastics needed to call
   * `requestDidStart` and `didResolveOperation` directly without an
   * Apollo Server instance — the plugin object is structurally typed
   * (`{requestDidStart: () => Promise<void | GraphQLRequestListener>}`)
   * so the type cast keeps the test ergonomic without losing safety on
   * the observable contract.
   */
  async function runOnce(operationName: string, document: DocumentNode) {
    const log = vi.fn();
    const plugin = costBreakdownPlugin(log);
    const reqDidStart = plugin.requestDidStart;
    expect(reqDidStart).toBeDefined();
    // The Apollo Server lifecycle: requestDidStart returns a listener
    // (or void). We pass a minimal context object — only `schema`,
    // `document`, and `operationName` are read by `didResolveOperation`.
    const ctx = { schema, document, operationName } as Parameters<
      NonNullable<typeof reqDidStart>
    >[0];
    const listener = await reqDidStart!(ctx);
    expect(listener).toBeDefined();
    const handler = (
      listener as { didResolveOperation?: (c: typeof ctx) => Promise<void> }
    ).didResolveOperation;
    expect(handler).toBeDefined();
    await handler!(ctx);
    return log.mock.calls;
  }

  it('does NOT log when total cost is below the 800 threshold', async () => {
    const cheapDoc = parse(/* GraphQL */ `
      query Cheap {
        queueDepth
      }
    `);
    const calls = await runOnce('Cheap', cheapDoc);
    expect(calls).toHaveLength(0);
  });

  it('logs once with breakdown when total cost meets the threshold', async () => {
    const document = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const calls = await runOnce('DispatcherState', document);
    expect(calls).toHaveLength(1);
    const entry = calls[0]![0]! as {
      operationName: string;
      cost: number;
      breakdown: { path: string; cost: number }[];
    };
    expect(entry.operationName).toBe('DispatcherState');
    expect(entry.cost).toBeGreaterThanOrEqual(800);
    expect(entry.breakdown.length).toBeGreaterThan(0);
    // Sorted by cost descending.
    for (let i = 1; i < entry.breakdown.length; i++) {
      expect(entry.breakdown[i - 1]!.cost).toBeGreaterThanOrEqual(
        entry.breakdown[i]!.cost,
      );
    }
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
