import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/lib/apollo-client', () => ({
  createApolloClient: () => ({
    // Minimal mock ApolloClient shape
    query: vi.fn(),
    mutate: vi.fn(),
    cache: { reset: vi.fn() },
    link: {},
    watchQuery: vi.fn(),
    subscribe: vi.fn(),
    readQuery: vi.fn(),
    writeQuery: vi.fn(),
    readFragment: vi.fn(),
    writeFragment: vi.fn(),
    resetStore: vi.fn(),
    clearStore: vi.fn(),
    stop: vi.fn(),
    addResolvers: vi.fn(),
    setResolvers: vi.fn(),
    getResolvers: vi.fn(),
    setLocalStateFragmentMatcher: vi.fn(),
    setLink: vi.fn(),
    disableNetworkFetches: false,
    version: '3.0',
    defaultOptions: {},
    typeDefs: undefined,
    extract: vi.fn(),
    restore: vi.fn(),
    reFetchObservableQueries: vi.fn(),
    refetchQueries: vi.fn(),
    getObservableQueries: vi.fn(),
    onResetStore: vi.fn(),
    onClearStore: vi.fn(),
  }),
}));

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    ApolloProvider: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="apollo-wrapper">{children}</div>
    ),
  };
});

import { ApolloProvider } from '../ApolloProvider';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ApolloProvider', () => {
  it('renders its children', () => {
    render(
      <ApolloProvider>
        <span>Child content</span>
      </ApolloProvider>,
    );
    expect(screen.getByText('Child content')).toBeInTheDocument();
  });

  it('wraps children in the Apollo provider', () => {
    render(
      <ApolloProvider>
        <span>Wrapped</span>
      </ApolloProvider>,
    );
    expect(screen.getByTestId('apollo-wrapper')).toBeInTheDocument();
  });
});
