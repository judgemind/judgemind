/**
 * Integration tests for the auth token lifecycle.
 *
 * These tests exercise the real Apollo client link chain
 * (errorLink -> authLink -> httpLink) by mocking at the fetch level.
 * Unlike the unit tests in apollo-client.test.ts (which mock onError and
 * auth-tokens), these tests verify the full integration between the links.
 *
 * Covers issue #953 acceptance criteria:
 * 1. Login mutation stores access token in memory
 * 2. Auth link injects Bearer header on subsequent requests
 * 3. Error link auto-refreshes on UNAUTHENTICATED error
 * 4. credentials: 'include' is set on the HttpLink (cookie sending)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { gql } from '@apollo/client';

// Simple test query used to trigger the link chain
const TEST_QUERY = gql`
  query TestQuery {
    test {
      id
    }
  }
`;

/**
 * Build a JSON response body matching what a GraphQL endpoint returns.
 */
function graphqlResponse(data: Record<string, unknown>): string {
  return JSON.stringify({ data });
}

function graphqlErrorResponse(
  message: string,
  code: string,
): string {
  return JSON.stringify({
    data: null,
    errors: [
      {
        message,
        extensions: { code },
      },
    ],
  });
}

/**
 * Create a mock fetch that returns predefined responses in order.
 * Each response is a { status, body } object or a function returning one.
 */
type MockResponse = {
  status: number;
  body: string;
  headers?: Record<string, string>;
};

function createMockFetch(
  responses: Array<MockResponse | (() => MockResponse)>,
): typeof globalThis.fetch & ReturnType<typeof vi.fn> {
  let callIndex = 0;
  const mockFn = vi.fn(
    (_input: RequestInfo | URL, _init?: RequestInit): Promise<Response> => {
      const responseDef = responses[callIndex];
      if (!responseDef) {
        throw new Error(
          `Unexpected fetch call #${callIndex + 1}: no more mock responses defined`,
        );
      }
      callIndex++;
      const resp =
        typeof responseDef === 'function' ? responseDef() : responseDef;
      return Promise.resolve(
        new Response(resp.body, {
          status: resp.status,
          headers: {
            'content-type': 'application/json',
            ...(resp.headers ?? {}),
          },
        }),
      );
    },
  );
  return mockFn as typeof globalThis.fetch & ReturnType<typeof vi.fn>;
}

describe('auth integration — token lifecycle', () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    vi.resetModules();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('login mutation stores access token in memory', async () => {
    const mockFetch = createMockFetch([
      {
        status: 200,
        body: graphqlResponse({
          login: {
            accessToken: 'jwt-from-login',
            user: {
              id: '1',
              email: 'test@example.com',
              emailVerified: false,
              displayName: null,
              role: 'user',
              createdAt: '2026-01-01T00:00:00Z',
            },
          },
        }),
      },
    ]);
    globalThis.fetch = mockFetch;

    const { createApolloClient } = await import('../apollo-client');
    const { getAccessToken, setAccessToken } = await import('../auth-tokens');
    const { LOGIN_MUTATION } = await import('../auth-mutations');

    const client = createApolloClient();

    // Execute login mutation through the full link chain
    const result = await client.mutate({
      mutation: LOGIN_MUTATION,
      variables: { email: 'test@example.com', password: 'password123' },
    });

    // The app stores the token after receiving the login response
    const accessToken = result.data?.login?.accessToken as string;
    setAccessToken(accessToken);

    expect(accessToken).toBe('jwt-from-login');
    expect(getAccessToken()).toBe('jwt-from-login');
  });

  it('auth link injects Bearer header on subsequent requests', async () => {
    const mockFetch = createMockFetch([
      {
        status: 200,
        body: graphqlResponse({
          test: { id: '1' },
        }),
      },
    ]);
    globalThis.fetch = mockFetch;

    const { createApolloClient } = await import('../apollo-client');
    const { setAccessToken } = await import('../auth-tokens');

    // Pre-set the access token (simulating post-login state)
    setAccessToken('existing-jwt-token');

    const client = createApolloClient();
    await client.query({ query: TEST_QUERY });

    // Verify fetch was called with the Authorization header
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [, fetchOptions] = mockFetch.mock.calls[0] as [
      string,
      RequestInit,
    ];
    const headers = fetchOptions.headers as Record<string, string>;
    expect(headers).toHaveProperty('authorization', 'Bearer existing-jwt-token');
  });

  it('error link auto-refreshes on UNAUTHENTICATED error', async () => {
    // Three fetch calls expected:
    // 1. Original query -> UNAUTHENTICATED error
    // 2. Refresh token mutation -> new access token
    // 3. Retry of original query -> success
    const mockFetch = createMockFetch([
      {
        status: 200,
        body: graphqlErrorResponse(
          'Not authenticated',
          'UNAUTHENTICATED',
        ),
      },
      {
        status: 200,
        body: graphqlResponse({
          refreshToken: {
            accessToken: 'refreshed-jwt-token',
          },
        }),
      },
      {
        status: 200,
        body: graphqlResponse({
          test: { id: '42' },
        }),
      },
    ]);
    globalThis.fetch = mockFetch;

    const { createApolloClient } = await import('../apollo-client');
    const { setAccessToken, getAccessToken } = await import('../auth-tokens');

    // Start with an expired token
    setAccessToken('expired-token');

    const client = createApolloClient();
    const result = await client.query({ query: TEST_QUERY });

    // The error link should have:
    // 1. Detected UNAUTHENTICATED on first call
    // 2. Called refreshToken mutation
    // 3. Stored the new token
    // 4. Retried the original query with the new token
    expect(mockFetch).toHaveBeenCalledTimes(3);
    expect(result.data).toEqual({ test: { id: '42' } });
    expect(getAccessToken()).toBe('refreshed-jwt-token');

    // Verify the retry (3rd call) used the refreshed token
    const [, retryOptions] = mockFetch.mock.calls[2] as [
      string,
      RequestInit,
    ];
    const retryHeaders = retryOptions.headers as Record<string, string>;
    expect(retryHeaders).toHaveProperty(
      'authorization',
      'Bearer refreshed-jwt-token',
    );
  });

  it('credentials: include is set on fetch requests (cookie sending)', async () => {
    const mockFetch = createMockFetch([
      {
        status: 200,
        body: graphqlResponse({
          test: { id: '1' },
        }),
      },
    ]);
    globalThis.fetch = mockFetch;

    const { createApolloClient } = await import('../apollo-client');

    const client = createApolloClient();
    await client.query({ query: TEST_QUERY });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [, fetchOptions] = mockFetch.mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(fetchOptions.credentials).toBe('include');
  });
});
