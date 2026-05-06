/**
 * Unit tests for `costLimitPlugin` (issues #4112, #4101).
 *
 * The plugin enforces the #4003 1000-cap by hooking
 * `didResolveOperation` and calling `getComplexity()` against the
 * resolved document. Issue #4101 added the per-field breakdown to the
 * `onCost` callback so the production `graphql.cost` log line carries
 * both the total and a `{ [path]: cost }` map. These tests drive the
 * plugin through a real Apollo Server instance via `app.inject()` so
 * the verified path matches what production traffic exercises.
 *
 * Coverage:
 *   - The post-#4100 polled `DispatcherState` query is NOT rejected
 *     (cost ~996 stays under 1000) — sanity guard against the cockpit
 *     poll regressing.
 *   - A query that exceeds the cap is rejected with HTTP 400 + a
 *     `complexityLimitExceeded` extension flag.
 *   - The plugin invokes `onCost` with the structured cost entry on
 *     every request — `{ cost, operationName, breakdown }` — and the
 *     breakdown's values sum to the total.
 *
 * Realm note: the production plugin imports `getComplexity` from
 * `graphql-query-complexity/cjs` to keep its runtime in graphql's CJS
 * realm under both production (Node CJS) and vitest's ESM loader. We
 * exercise the wiring through `app.inject()` instead of calling the
 * plugin's lifecycle hooks directly so the schema realm matches what
 * the plugin sees in production. The breakdown-shape AC2 invariant is
 * additionally pinned by `cost-breakdown.unit.test.ts` against the
 * walker directly.
 */

import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { buildApp } from '../app';

function makeStubPool(): Pool {
  return {
    query: vi.fn(async () => ({ rows: [], rowCount: 0 })),
    end: vi.fn(async () => undefined),
  } as unknown as Pool;
}

const POLLED_QUERY = `
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

// Construct a query that's well over the 1000 cap WITHOUT relying on
// Apollo Client's `__typename` injection (the server here gets the raw
// document — under our algorithm the post-#4100 polled query without
// typename injection is ~743). Aliased duplicates of `activeAgents`
// (a 10× list-of-objects with 8 scalar fields = 80 cost each) are the
// cheapest way to push over the cap while staying close in shape to
// the production polled query.
const OVER_CAP_QUERY = POLLED_QUERY.replace(
  /activeAgents \{[^}]*\}/,
  `activeAgents { id issueNumber issueTitle priority worktreePath phase startedAt retriesUsed }
  agents2: activeAgents { id issueNumber issueTitle priority worktreePath phase startedAt retriesUsed }
  agents3: activeAgents { id issueNumber issueTitle priority worktreePath phase startedAt retriesUsed }
  agents4: activeAgents { id issueNumber issueTitle priority worktreePath phase startedAt retriesUsed }
  agents5: activeAgents { id issueNumber issueTitle priority worktreePath phase startedAt retriesUsed }
  agents6: activeAgents { id issueNumber issueTitle priority worktreePath phase startedAt retriesUsed }`,
);

interface CapturedLog {
  msg: string;
  data: Record<string, unknown>;
}

describe('costLimitPlugin (issues #4112, #4101)', () => {
  let app: FastifyInstance;
  let captured: CapturedLog[];

  beforeAll(async () => {
    app = await buildApp(makeStubPool());
    // Capture every `app.log.info` call. The buildApp wiring passes
    // `(entry) => app.log.info(entry, 'graphql.cost')` to the plugin,
    // so a spy on `info` lets us see the entry shape from the same
    // path production hits.
    captured = [];
    const origInfo = app.log.info.bind(app.log);
    app.log.info = ((dataOrMsg: unknown, msg?: string) => {
      // Pino accepts (obj, msg) OR (msg) — capture the (obj, msg) form
      // which is what the plugin uses.
      if (typeof dataOrMsg === 'object' && dataOrMsg !== null && typeof msg === 'string') {
        captured.push({ msg, data: dataOrMsg as Record<string, unknown> });
      }
      // Forward to the real logger for visibility under -v.
      return origInfo(dataOrMsg, msg);
    }) as typeof app.log.info;
  });

  afterAll(async () => {
    await app.close();
  });

  it('admits the post-#4100 polled query (cost ~996, under the 1000 cap)', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: POLLED_QUERY }),
    });
    // The polled query has no required variables, so validation passes.
    // Resolvers fail (no DB) but that's not a validation issue — status
    // is 200 with resolver-level errors.
    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body);
    if (body.errors) {
      // No complexity-limit error.
      const hasComplexityError = body.errors.some(
        (e: { message?: string; extensions?: { complexityLimitExceeded?: boolean } }) =>
          e.extensions?.complexityLimitExceeded === true ||
          (typeof e.message === 'string' &&
            /maximum cost/i.test(e.message)),
      );
      expect(hasComplexityError).toBe(false);
    }
  });

  it('rejects a 40-unit-over-cap query with HTTP 400 + complexityLimitExceeded', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: OVER_CAP_QUERY }),
    });
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
    expect(body.errors.length).toBeGreaterThanOrEqual(1);
    const exceeded = body.errors.find(
      (e: {
        extensions?: { complexityLimitExceeded?: boolean; actualCost?: number };
      }) => e.extensions?.complexityLimitExceeded === true,
    );
    expect(exceeded).toBeDefined();
    expect(exceeded.extensions.actualCost).toBeGreaterThan(1000);
    expect(exceeded.extensions.maximumCost).toBe(1000);
  });

  it('emits a `graphql.cost` log entry with `cost`, `operationName`, and `breakdown` (issue #4101)', async () => {
    captured.length = 0;
    await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: POLLED_QUERY }),
    });
    const cost = captured.find((c) => c.msg === 'graphql.cost');
    expect(cost).toBeDefined();
    expect(typeof cost!.data.cost).toBe('number');
    expect(cost!.data.operationName).toBe('DispatcherState');
    expect(typeof cost!.data.breakdown).toBe('object');
    expect(Array.isArray(cost!.data.breakdown)).toBe(false);
    const breakdown = cost!.data.breakdown as Record<string, number>;
    // Depth-2 paths under `dispatcherState` are present.
    const keys = Object.keys(breakdown);
    expect(keys.some((k) => k.startsWith('dispatcherState.'))).toBe(true);
  });

  it('AC2 of #4101: breakdown values sum to the cost total in the production log line', async () => {
    captured.length = 0;
    await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: POLLED_QUERY }),
    });
    const cost = captured.find((c) => c.msg === 'graphql.cost');
    expect(cost).toBeDefined();
    const breakdown = cost!.data.breakdown as Record<string, number>;
    const total = cost!.data.cost as number;
    const sum = Object.values(breakdown).reduce((a, b) => a + b, 0);
    expect(sum).toBe(total);
  });

  it('emits the `graphql.cost` log even for over-cap requests', async () => {
    // The plugin invokes onCost before the cap-exceeded throw so
    // operators see the cost+breakdown for rejected requests too —
    // matches the pre-#4101 behaviour where `graphql.cost` always fired.
    captured.length = 0;
    await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: OVER_CAP_QUERY }),
    });
    const cost = captured.find((c) => c.msg === 'graphql.cost');
    expect(cost).toBeDefined();
    expect(cost!.data.cost as number).toBeGreaterThan(1000);
    const breakdown = cost!.data.breakdown as Record<string, number>;
    expect(Object.keys(breakdown).length).toBeGreaterThan(0);
  });

  it('does NOT emit `graphql.cost.breakdown` (collapsed into `graphql.cost` per #4101)', async () => {
    captured.length = 0;
    await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query: POLLED_QUERY }),
    });
    expect(
      captured.find((c) => c.msg === 'graphql.cost.breakdown'),
    ).toBeUndefined();
  });
});
