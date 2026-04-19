import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks — declared before imports
// ---------------------------------------------------------------------------

let mockAuthResult: {
  user: {
    id: string;
    email: string;
    emailVerified: boolean;
    displayName: string | null;
    role: string;
    createdAt: string;
  } | null;
  loading: boolean;
  setUser: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
};

let mockPathname = '/search';

vi.mock('@/providers/AuthProvider', () => ({
  useAuth: () => mockAuthResult,
}));

vi.mock('next/navigation', () => ({
  usePathname: () => mockPathname,
}));

// ---------------------------------------------------------------------------
// Import after mocks
// ---------------------------------------------------------------------------

import { Sidebar } from '@/components/layout/Sidebar';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeUser(role = 'user') {
  return {
    id: '1',
    email: 'test@example.com',
    emailVerified: true,
    displayName: 'Test User',
    role,
    createdAt: '2025-01-01T00:00:00Z',
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Sidebar', () => {
  beforeEach(() => {
    mockAuthResult = {
      user: null,
      loading: false,
      setUser: vi.fn(),
      logout: vi.fn(),
    };
    mockPathname = '/search';
  });

  it('renders Explore and Research sections', () => {
    render(<Sidebar />);
    expect(screen.getByText('Explore')).toBeInTheDocument();
    expect(screen.getByText('Research')).toBeInTheDocument();
    expect(screen.getByText('Search Rulings')).toBeInTheDocument();
    expect(screen.getByText('Latest Rulings')).toBeInTheDocument();
    expect(screen.getByText('Cases')).toBeInTheDocument();
    expect(screen.getByText('Judges')).toBeInTheDocument();
  });

  it('does not show admin section for unauthenticated users', () => {
    render(<Sidebar />);
    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
    expect(screen.queryByText('Data Health')).not.toBeInTheDocument();
  });

  it('does not show admin section for regular users', () => {
    mockAuthResult.user = makeUser('user');
    render(<Sidebar />);
    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
    expect(screen.queryByText('Data Health')).not.toBeInTheDocument();
  });

  it('shows admin section for admin users', () => {
    mockAuthResult.user = makeUser('admin');
    render(<Sidebar />);
    expect(screen.getByText('Admin')).toBeInTheDocument();
    expect(screen.getByText('Data Health')).toBeInTheDocument();
  });

  it('admin Data Health link points to /admin/data-quality/', () => {
    mockAuthResult.user = makeUser('admin');
    render(<Sidebar />);
    const link = screen.getByText('Data Health');
    const href = link.closest('a')?.getAttribute('href') ?? '';
    expect(href).toMatch(/^\/admin\/data-quality\/?$/);
  });

  it('shows Dispatcher link for admin users', () => {
    mockAuthResult.user = makeUser('admin');
    render(<Sidebar />);
    expect(screen.getByText('Dispatcher')).toBeInTheDocument();
  });

  it('admin Dispatcher link points to /admin/dispatcher/', () => {
    mockAuthResult.user = makeUser('admin');
    render(<Sidebar />);
    const link = screen.getByText('Dispatcher');
    const href = link.closest('a')?.getAttribute('href') ?? '';
    expect(href).toMatch(/^\/admin\/dispatcher\/?$/);
  });

  it('does not show Dispatcher link for non-admin users', () => {
    mockAuthResult.user = makeUser('user');
    render(<Sidebar />);
    expect(screen.queryByText('Dispatcher')).not.toBeInTheDocument();
  });

  it('highlights the active route with accent styling', () => {
    mockPathname = '/search';
    render(<Sidebar />);
    const searchLink = screen.getByText('Search Rulings').closest('a');
    expect(searchLink?.className).toContain('bg-accent');
    expect(searchLink?.className).toContain('text-accent-foreground');
  });

  it('does not highlight inactive routes with accent styling', () => {
    mockPathname = '/search';
    render(<Sidebar />);
    const rulingsLink = screen.getByText('Latest Rulings').closest('a');
    const classes = rulingsLink?.className.split(' ') ?? [];
    expect(classes).not.toContain('bg-accent');
  });

  it('calls onLinkClick when a link is clicked', async () => {
    const onLinkClick = vi.fn();
    render(<Sidebar onLinkClick={onLinkClick} />);
    const link = screen.getByText('Search Rulings');
    link.click();
    expect(onLinkClick).toHaveBeenCalled();
  });

  it('does not show admin section while auth is loading', () => {
    mockAuthResult.loading = true;
    render(<Sidebar />);
    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
  });

  it('renders navigation icons alongside link text', () => {
    render(<Sidebar />);
    const searchLink = screen.getByText('Search Rulings').closest('a');
    expect(searchLink?.querySelector('svg')).toBeInTheDocument();
  });

  it('uses shadcn Button components for navigation links', () => {
    render(<Sidebar />);
    const searchLink = screen.getByText('Search Rulings').closest('a');
    expect(searchLink).toBeInTheDocument();
    expect(searchLink?.className).toContain('justify-start');
  });
});
