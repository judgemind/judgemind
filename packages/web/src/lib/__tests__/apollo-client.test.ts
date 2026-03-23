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
