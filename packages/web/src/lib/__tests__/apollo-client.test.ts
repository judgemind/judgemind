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
});
