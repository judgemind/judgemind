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

vi.mock('@/components/layout/Sidebar', () => ({
  Sidebar: () => <aside data-testid="sidebar">Sidebar</aside>,
  DesktopSidebar: () => <aside data-testid="sidebar">Sidebar</aside>,
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

  it('renders the Sidebar component', () => {
    render(<RootLayout>Content</RootLayout>);
    expect(screen.getByTestId('sidebar')).toBeInTheDocument();
  });

  it('wraps content in a main element', () => {
    render(<RootLayout>Page content</RootLayout>);
    const main = screen.getByRole('main');
    expect(main).toBeInTheDocument();
    expect(main).toHaveTextContent('Page content');
  });
});
