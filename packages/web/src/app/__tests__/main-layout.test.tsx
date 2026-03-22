import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/components/layout/Sidebar', () => ({
  Sidebar: () => <aside data-testid="sidebar">Sidebar</aside>,
  DesktopSidebar: () => <aside data-testid="desktop-sidebar">Desktop Sidebar</aside>,
}));

import MainLayout from '../(main)/layout';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('MainLayout', () => {
  it('renders children', () => {
    render(<MainLayout>Page content</MainLayout>);
    expect(screen.getByText('Page content')).toBeInTheDocument();
  });

  it('renders the DesktopSidebar', () => {
    render(<MainLayout>Content</MainLayout>);
    expect(screen.getByTestId('desktop-sidebar')).toBeInTheDocument();
  });

  it('wraps content in a main element with id main-content', () => {
    render(<MainLayout>Page content</MainLayout>);
    const main = screen.getByRole('main');
    expect(main).toBeInTheDocument();
    expect(main).toHaveTextContent('Page content');
    expect(main).toHaveAttribute('id', 'main-content');
  });

  it('uses flex layout for sidebar and main content', () => {
    const { container } = render(<MainLayout>Content</MainLayout>);
    const wrapper = container.firstElementChild;
    expect(wrapper).toHaveClass('flex', 'flex-1');
  });
});
