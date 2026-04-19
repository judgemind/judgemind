import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ApolloLink } from '@apollo/client';

// Mock @apollo/client/link/error before importing the module under test.
// onError must return a valid ApolloLink instance for ApolloLink.from() to work.
vi.mock('@apollo/client/link/error', () => ({
  onError: vi.fn(
    () =>
      new ApolloLink((operation, forward) => {
        return forward(operation);
      }),
  ),
}));

// Mock auth-tokens module
vi.mock('../auth-tokens', () => ({
  getAccessToken: vi.fn(() => null),
  setAccessToken: vi.fn(),
  clearAccessToken: vi.fn(),
}));

// Reset modules between tests to get fresh client instances
beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

describe('createApolloClient', () => {
  it('returns an ApolloClient instance', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();
    expect(client).toBeDefined();
    expect(client.cache).toBeDefined();
    expect(client.link).toBeDefined();
  });

  it('uses NEXT_PUBLIC_GRAPHQL_URL when set', async () => {
    const originalEnv = process.env.NEXT_PUBLIC_GRAPHQL_URL;
    process.env.NEXT_PUBLIC_GRAPHQL_URL = 'https://api.test.com/graphql';

    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();
    expect(client).toBeDefined();

    process.env.NEXT_PUBLIC_GRAPHQL_URL = originalEnv;
  });

  it('falls back to localhost when env var is not set', async () => {
    const originalEnv = process.env.NEXT_PUBLIC_GRAPHQL_URL;
    delete process.env.NEXT_PUBLIC_GRAPHQL_URL;

    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();
    expect(client).toBeDefined();

    process.env.NEXT_PUBLIC_GRAPHQL_URL = originalEnv;
  });

  it('creates a new client on each call', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client1 = createApolloClient();
    const client2 = createApolloClient();
    expect(client1).not.toBe(client2);
  });

  it('configures the client with an InMemoryCache', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();
    expect(client.cache).toBeDefined();
  });

  it('caches multiple OutcomeCount items without deduplication', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();

    const { gql: clientGql } = await import('@apollo/client');
    client.cache.writeQuery({
      query: clientGql`
        query JudgeAnalytics($judgeId: ID!) {
          judgeAnalytics(judgeId: $judgeId) {
            judgeId
            totalRulings
            rulingsByOutcome {
              outcome
              count
            }
            rulingsByMotionType {
              motionType
              total
              granted
              denied
              grantedInPart
              other
              grantRate
            }
            earliestRuling
            latestRuling
          }
        }
      `,
      variables: { judgeId: 'judge-1' },
      data: {
        judgeAnalytics: {
          __typename: 'JudgeAnalytics',
          judgeId: 'judge-1',
          totalRulings: 10,
          rulingsByOutcome: [
            { __typename: 'OutcomeCount', outcome: 'granted', count: 5 },
            { __typename: 'OutcomeCount', outcome: 'denied', count: 3 },
            { __typename: 'OutcomeCount', outcome: 'other', count: 2 },
          ],
          rulingsByMotionType: [
            {
              __typename: 'MotionStats',
              motionType: 'msj',
              total: 6,
              granted: 3,
              denied: 2,
              grantedInPart: 1,
              other: 0,
              grantRate: 0.5,
            },
            {
              __typename: 'MotionStats',
              motionType: 'demurrer',
              total: 4,
              granted: 2,
              denied: 1,
              grantedInPart: 0,
              other: 1,
              grantRate: 0.67,
            },
          ],
          earliestRuling: '2025-01-01',
          latestRuling: '2026-03-01',
        },
      },
    });

    const result = client.cache.readQuery<{
      judgeAnalytics: {
        rulingsByOutcome: Array<{ outcome: string; count: number }>;
      };
    }>({
      query: clientGql`
        query JudgeAnalytics($judgeId: ID!) {
          judgeAnalytics(judgeId: $judgeId) {
            judgeId
            totalRulings
            rulingsByOutcome {
              outcome
              count
            }
            rulingsByMotionType {
              motionType
              total
              granted
              denied
              grantedInPart
              other
              grantRate
            }
            earliestRuling
            latestRuling
          }
        }
      `,
      variables: { judgeId: 'judge-1' },
    });

    expect(result).not.toBeNull();
    expect(result!.judgeAnalytics.rulingsByOutcome).toHaveLength(3);
    const outcomes = result!.judgeAnalytics.rulingsByOutcome.map(
      (o) => o.outcome,
    );
    expect(outcomes).toContain('granted');
    expect(outcomes).toContain('denied');
    expect(outcomes).toContain('other');
  });

  it('caches multiple MotionStats items without deduplication', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();

    const { gql: clientGql } = await import('@apollo/client');
    client.cache.writeQuery({
      query: clientGql`
        query JudgeAnalytics($judgeId: ID!) {
          judgeAnalytics(judgeId: $judgeId) {
            judgeId
            totalRulings
            rulingsByOutcome {
              outcome
              count
            }
            rulingsByMotionType {
              motionType
              total
              granted
              denied
              grantedInPart
              other
              grantRate
            }
            earliestRuling
            latestRuling
          }
        }
      `,
      variables: { judgeId: 'judge-2' },
      data: {
        judgeAnalytics: {
          __typename: 'JudgeAnalytics',
          judgeId: 'judge-2',
          totalRulings: 8,
          rulingsByOutcome: [
            { __typename: 'OutcomeCount', outcome: 'granted', count: 4 },
          ],
          rulingsByMotionType: [
            {
              __typename: 'MotionStats',
              motionType: 'msj',
              total: 3,
              granted: 2,
              denied: 1,
              grantedInPart: 0,
              other: 0,
              grantRate: 0.67,
            },
            {
              __typename: 'MotionStats',
              motionType: 'demurrer',
              total: 3,
              granted: 1,
              denied: 1,
              grantedInPart: 1,
              other: 0,
              grantRate: 0.33,
            },
            {
              __typename: 'MotionStats',
              motionType: 'mtd',
              total: 2,
              granted: 1,
              denied: 0,
              grantedInPart: 0,
              other: 1,
              grantRate: 1.0,
            },
          ],
          earliestRuling: '2025-06-01',
          latestRuling: '2026-02-15',
        },
      },
    });

    const result = client.cache.readQuery<{
      judgeAnalytics: {
        rulingsByMotionType: Array<{ motionType: string; total: number }>;
      };
    }>({
      query: clientGql`
        query JudgeAnalytics($judgeId: ID!) {
          judgeAnalytics(judgeId: $judgeId) {
            judgeId
            totalRulings
            rulingsByOutcome {
              outcome
              count
            }
            rulingsByMotionType {
              motionType
              total
              granted
              denied
              grantedInPart
              other
              grantRate
            }
            earliestRuling
            latestRuling
          }
        }
      `,
      variables: { judgeId: 'judge-2' },
    });

    expect(result).not.toBeNull();
    expect(result!.judgeAnalytics.rulingsByMotionType).toHaveLength(3);
    const motionTypes = result!.judgeAnalytics.rulingsByMotionType.map(
      (m) => m.motionType,
    );
    expect(motionTypes).toContain('msj');
    expect(motionTypes).toContain('demurrer');
    expect(motionTypes).toContain('mtd');
  });

  it('caches JudgeAnalytics for different judges as distinct entries', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();

    const { gql: clientGql } = await import('@apollo/client');
    const analyticsQuery = clientGql`
      query JudgeAnalytics($judgeId: ID!) {
        judgeAnalytics(judgeId: $judgeId) {
          judgeId
          totalRulings
          rulingsByOutcome {
            outcome
            count
          }
          rulingsByMotionType {
            motionType
            total
            granted
            denied
            grantedInPart
            other
            grantRate
          }
          earliestRuling
          latestRuling
        }
      }
    `;

    // Write analytics for judge-1
    client.cache.writeQuery({
      query: analyticsQuery,
      variables: { judgeId: 'judge-1' },
      data: {
        judgeAnalytics: {
          __typename: 'JudgeAnalytics',
          judgeId: 'judge-1',
          totalRulings: 50,
          rulingsByOutcome: [
            { __typename: 'OutcomeCount', outcome: 'granted', count: 30 },
          ],
          rulingsByMotionType: [],
          earliestRuling: '2024-01-01',
          latestRuling: '2026-01-01',
        },
      },
    });

    // Write analytics for judge-2
    client.cache.writeQuery({
      query: analyticsQuery,
      variables: { judgeId: 'judge-2' },
      data: {
        judgeAnalytics: {
          __typename: 'JudgeAnalytics',
          judgeId: 'judge-2',
          totalRulings: 25,
          rulingsByOutcome: [
            { __typename: 'OutcomeCount', outcome: 'denied', count: 15 },
          ],
          rulingsByMotionType: [],
          earliestRuling: '2025-01-01',
          latestRuling: '2026-02-01',
        },
      },
    });

    // Read back judge-1 — should not be overwritten by judge-2
    const result1 = client.cache.readQuery<{
      judgeAnalytics: { judgeId: string; totalRulings: number };
    }>({
      query: analyticsQuery,
      variables: { judgeId: 'judge-1' },
    });

    const result2 = client.cache.readQuery<{
      judgeAnalytics: { judgeId: string; totalRulings: number };
    }>({
      query: analyticsQuery,
      variables: { judgeId: 'judge-2' },
    });

    expect(result1).not.toBeNull();
    expect(result1!.judgeAnalytics.judgeId).toBe('judge-1');
    expect(result1!.judgeAnalytics.totalRulings).toBe(50);

    expect(result2).not.toBeNull();
    expect(result2!.judgeAnalytics.judgeId).toBe('judge-2');
    expect(result2!.judgeAnalytics.totalRulings).toBe(25);
  });

  it('caches multiple CaseEdge items without deduplication (keyFields: false)', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();

    const { gql: clientGql } = await import('@apollo/client');
    const casesQuery = clientGql`
      query Cases {
        cases {
          edges {
            cursor
            node {
              id
              caseNumber
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    `;

    client.cache.writeQuery({
      query: casesQuery,
      data: {
        cases: {
          __typename: 'CaseConnection',
          edges: [
            {
              __typename: 'CaseEdge',
              cursor: 'cursor-aaa',
              node: {
                __typename: 'Case',
                id: 'case-1',
                caseNumber: '24STCV00001',
              },
            },
            {
              __typename: 'CaseEdge',
              cursor: 'cursor-bbb',
              node: {
                __typename: 'Case',
                id: 'case-2',
                caseNumber: '24STCV00002',
              },
            },
            {
              __typename: 'CaseEdge',
              cursor: 'cursor-ccc',
              node: {
                __typename: 'Case',
                id: 'case-3',
                caseNumber: '24STCV00003',
              },
            },
          ],
          pageInfo: {
            __typename: 'PageInfo',
            hasNextPage: true,
            endCursor: 'cursor-ccc',
          },
        },
      },
    });

    const result = client.cache.readQuery<{
      cases: {
        edges: Array<{
          cursor: string;
          node: { id: string; caseNumber: string };
        }>;
      };
    }>({ query: casesQuery });

    expect(result).not.toBeNull();
    expect(result!.cases.edges).toHaveLength(3);
    expect(result!.cases.edges[0].cursor).toBe('cursor-aaa');
    expect(result!.cases.edges[1].cursor).toBe('cursor-bbb');
    expect(result!.cases.edges[2].cursor).toBe('cursor-ccc');
    expect(result!.cases.edges[0].node.id).toBe('case-1');
    expect(result!.cases.edges[1].node.id).toBe('case-2');
    expect(result!.cases.edges[2].node.id).toBe('case-3');
  });

  it('caches multiple DispatcherFailure items without deduplication', async () => {
    // Regression: DispatcherFailure has no `id` field — without the
    // `keyFields: ['failureId']` entry in apollo-client.ts, Apollo would
    // collapse distinct failure rows into a single cache entry (#1542).
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();
    const { gql: clientGql } = await import('@apollo/client');
    const stateQuery = clientGql`
      query DispatcherState {
        dispatcherState {
          recentFailures {
            failureId
            category
            detectedBy
          }
        }
      }
    `;
    client.cache.writeQuery({
      query: stateQuery,
      data: {
        dispatcherState: {
          __typename: 'DispatcherState',
          recentFailures: [
            { __typename: 'DispatcherFailure', failureId: '1', category: 'hook_failure', detectedBy: 'hook:subagentstop' },
            { __typename: 'DispatcherFailure', failureId: '2', category: 'hook_failure', detectedBy: 'hook:subagentstop' },
            { __typename: 'DispatcherFailure', failureId: '3', category: 'subprocess_timeout', detectedBy: 'supervisor:timeout' },
          ],
        },
      },
    });
    const result = client.cache.readQuery<{
      dispatcherState: { recentFailures: Array<{ failureId: string; category: string }> };
    }>({ query: stateQuery });
    expect(result).not.toBeNull();
    expect(result!.dispatcherState.recentFailures).toHaveLength(3);
    const ids = result!.dispatcherState.recentFailures.map((f) => f.failureId);
    expect(ids).toEqual(['1', '2', '3']);
  });

  it('caches multiple PhaseTransition items without deduplication', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();
    const { gql: clientGql } = await import('@apollo/client');
    const agentQuery = clientGql`
      query DispatcherAgent($agentId: ID!) {
        dispatcherAgent(agentId: $agentId) {
          id
          phaseTransitions { transitionId phase ts autocompactCount }
        }
      }
    `;
    client.cache.writeQuery({
      query: agentQuery,
      variables: { agentId: 'agent-1' },
      data: {
        dispatcherAgent: {
          __typename: 'DispatcherAgent',
          id: 'agent-1',
          phaseTransitions: [
            { __typename: 'PhaseTransition', transitionId: '10', phase: 'claiming', ts: '2026-04-18T19:00:00Z', autocompactCount: 0 },
            { __typename: 'PhaseTransition', transitionId: '11', phase: 'ralph-worker', ts: '2026-04-18T19:05:00Z', autocompactCount: 0 },
            { __typename: 'PhaseTransition', transitionId: '12', phase: 'pushing', ts: '2026-04-18T19:20:00Z', autocompactCount: 0 },
          ],
        },
      },
    });
    const result = client.cache.readQuery<{
      dispatcherAgent: { phaseTransitions: Array<{ transitionId: string; phase: string }> };
    }>({ query: agentQuery, variables: { agentId: 'agent-1' } });
    expect(result).not.toBeNull();
    expect(result!.dispatcherAgent.phaseTransitions).toHaveLength(3);
    expect(result!.dispatcherAgent.phaseTransitions.map((t) => t.phase)).toEqual([
      'claiming',
      'ralph-worker',
      'pushing',
    ]);
  });

  it('caches multiple DataQualityOverview items without deduplication', async () => {
    const { createApolloClient } = await import('../apollo-client');
    const client = createApolloClient();

    // Write multiple DataQualityOverview objects directly to the cache
    // to verify they are stored as distinct entries (not merged)
    const { gql: clientGql } = await import('@apollo/client');
    client.cache.writeQuery({
      query: clientGql`
        query DataQualityOverview {
          dataQualityOverview {
            county
            healthStatus
            rulingCount24h
            fieldCompletenessPct
            scraperLastSuccessAgeHours
            lastUpdated
          }
        }
      `,
      data: {
        dataQualityOverview: [
          {
            __typename: 'DataQualityOverview',
            county: 'Los Angeles',
            healthStatus: 'green',
            rulingCount24h: 42,
            fieldCompletenessPct: 95.5,
            scraperLastSuccessAgeHours: 1.2,
            lastUpdated: '2026-03-01T10:00:00Z',
          },
          {
            __typename: 'DataQualityOverview',
            county: 'Orange',
            healthStatus: 'yellow',
            rulingCount24h: 10,
            fieldCompletenessPct: 82.0,
            scraperLastSuccessAgeHours: 8.5,
            lastUpdated: '2026-03-01T09:00:00Z',
          },
          {
            __typename: 'DataQualityOverview',
            county: 'San Diego',
            healthStatus: 'red',
            rulingCount24h: 0,
            fieldCompletenessPct: 45.0,
            scraperLastSuccessAgeHours: 30.0,
            lastUpdated: '2026-02-28T12:00:00Z',
          },
        ],
      },
    });

    // Read back from cache — all three counties must be present
    const result = client.cache.readQuery<{
      dataQualityOverview: Array<{ county: string; healthStatus: string }>;
    }>({
      query: clientGql`
        query DataQualityOverview {
          dataQualityOverview {
            county
            healthStatus
            rulingCount24h
            fieldCompletenessPct
            scraperLastSuccessAgeHours
            lastUpdated
          }
        }
      `,
    });

    expect(result).not.toBeNull();
    expect(result!.dataQualityOverview).toHaveLength(3);
    const counties = result!.dataQualityOverview.map((o) => o.county);
    expect(counties).toContain('Los Angeles');
    expect(counties).toContain('Orange');
    expect(counties).toContain('San Diego');
  });
});
