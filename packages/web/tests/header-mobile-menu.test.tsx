import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
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

vi.mock('@/providers/AuthProvider', () => ({
  useAuth: () => mockAuthResult,
}));

vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ theme: 'light', toggle: vi.fn() }),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/search',
}));

// ---------------------------------------------------------------------------
// Import after mocks
// ---------------------------------------------------------------------------

import { Header } from '@/components/layout/Header';

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

describe('Header mobile menu', () => {
  beforeEach(() => {
    mockAuthResult = {
      user: makeUser(),
      loading: false,
      setUser: vi.fn(),
      logout: vi.fn(),
    };
  });

  it('renders a hamburger menu button', () => {
    render(<Header />);
    const button = screen.getByLabelText('Toggle menu');
    expect(button).toBeInTheDocument();
  });

  it('hamburger button is hidden on large screens via CSS class', () => {
    render(<Header />);
    const button = screen.getByLabelText('Toggle menu');
    expect(button.className).toContain('lg:hidden');
  });

  it('does not show mobile sidebar initially', () => {
    render(<Header />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('opens mobile sidebar when hamburger is clicked', () => {
    render(<Header />);
    const button = screen.getByLabelText('Toggle menu');
    fireEvent.click(button);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('closes mobile sidebar when close button is clicked', () => {
    render(<Header />);
    fireEvent.click(screen.getByLabelText('Toggle menu'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Close menu'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('closes mobile sidebar when overlay backdrop is clicked', () => {
    render(<Header />);
    fireEvent.click(screen.getByLabelText('Toggle menu'));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();

    // Click the backdrop (the outer dialog element)
    const backdrop = screen.getByTestId('mobile-menu-backdrop');
    fireEvent.click(backdrop);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows admin section in mobile menu for admin users', () => {
    mockAuthResult.user = makeUser('admin');
    render(<Header />);
    fireEvent.click(screen.getByLabelText('Toggle menu'));
    expect(screen.getByText('Admin')).toBeInTheDocument();
    expect(screen.getByText('Data Health')).toBeInTheDocument();
  });

  it('does not show admin section in mobile menu for regular users', () => {
    mockAuthResult.user = makeUser('user');
    render(<Header />);
    fireEvent.click(screen.getByLabelText('Toggle menu'));
    expect(screen.queryByText('Admin')).not.toBeInTheDocument();
  });
});
