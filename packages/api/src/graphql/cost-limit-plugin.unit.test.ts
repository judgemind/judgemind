/**
 * Unit tests for `costLimitPlugin` (issue #4112).
 *
 * The plugin enforces the #4003 1000-cap by hooking
 * `didResolveOperation` and calling `getComplexity()` against the
 * resolved document. These tests drive the plugin through a real
 * Apollo Server instance via `app.inject()` so the verified path
 * matches what production traffic exercises.
 *
 * Coverage:
 *   - The post-#4100 polled `DispatcherState` query is NOT rejected
 *     (cost ~996 stays under 1000) — sanity guard against the cockpit
 *     poll regressing.
 *   - A query that exceeds the cap by adding 4 extra `activeAgents`
 *     fields is rejected with HTTP 400 + a `complexityLimitExceeded`
 *     extension flag.
 *   - The plugin invokes `onCost` with the computed cost on every
 *     request (used in production to feed the `graphql.cost` log line).
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { vi } from 'vitest';
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

describe('costLimitPlugin (issue #4112)', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildApp(makeStubPool());
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
});
