import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks — provider and layout components are thin wrappers; mock them to
// isolate the layout structure.
// ---------------------------------------------------------------------------

vi.mock('@/providers/ThemeProvider', () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="theme-provider">{children}</div>
  ),
}));

vi.mock('@/providers/ApolloProvider', () => ({
  ApolloProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="apollo-provider">{children}</div>
  ),
}));

vi.mock('@/providers/AuthProvider', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="auth-provider">{children}</div>
  ),
}));

vi.mock('@/components/layout/Header', () => ({
  Header: () => <header data-testid="header">Header</header>,
}));

import RootLayout from '../layout';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RootLayout', () => {
  it('renders children within the provider stack', () => {
    render(<RootLayout>Page content</RootLayout>);
    expect(screen.getByText('Page content')).toBeInTheDocument();
  });

  it('nests providers in correct order: Theme > Apollo > Auth', () => {
    render(<RootLayout>Content</RootLayout>);
    const theme = screen.getByTestId('theme-provider');
    const apollo = screen.getByTestId('apollo-provider');
    const auth = screen.getByTestId('auth-provider');
    // Theme wraps Apollo wraps Auth
    expect(theme).toContainElement(apollo);
    expect(apollo).toContainElement(auth);
  });

  it('renders the Header component', () => {
    render(<RootLayout>Content</RootLayout>);
    expect(screen.getByTestId('header')).toBeInTheDocument();
  });

  it('does not render the Sidebar (sidebar is in route group layouts)', () => {
    render(<RootLayout>Content</RootLayout>);
    expect(screen.queryByTestId('sidebar')).not.toBeInTheDocument();
  });

  it('does not render a main element (main is in route group layouts)', () => {
    render(<RootLayout>Page content</RootLayout>);
    expect(screen.queryByRole('main')).not.toBeInTheDocument();
    // Children are still rendered directly
    expect(screen.getByText('Page content')).toBeInTheDocument();
  });

  it('renders a skip-to-main-content link as the first child of body', () => {
    render(<RootLayout>Content</RootLayout>);
    const skipLink = screen.getByText('Skip to main content');
    expect(skipLink).toBeInTheDocument();
    expect(skipLink.tagName).toBe('A');
    expect(skipLink).toHaveAttribute('href', '#main-content');
  });
});
