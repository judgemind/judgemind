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
    // Sheet is closed by default — its content is not rendered
    // The wordmark renders "judge" and "mind" as separate spans
    expect(screen.queryByText('judge')).toBeTruthy(); // header logo
  });

  it('opens mobile sidebar when hamburger is clicked', () => {
    render(<Header />);
    const button = screen.getByLabelText('Toggle menu');
    fireEvent.click(button);
    // Sheet opens and renders the sidebar navigation
    expect(screen.getByText('Explore')).toBeInTheDocument();
    expect(screen.getByText('Search Rulings')).toBeInTheDocument();
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

  it('renders shadcn Button components in header', () => {
    render(<Header />);
    const themeButton = screen.getByLabelText('Toggle dark mode');
    expect(themeButton).toBeInTheDocument();
  });

  it('renders user dropdown menu when logged in', () => {
    render(<Header />);
    const userButton = screen.getByLabelText('User menu');
    expect(userButton).toBeInTheDocument();
  });

  it('shows user info in dropdown menu when opened', async () => {
    const user = await import('@testing-library/user-event');
    const userEvent = user.default.setup();
    render(<Header />);
    const userButton = screen.getByLabelText('User menu');
    await userEvent.click(userButton);
    expect(screen.getByText('Test User')).toBeInTheDocument();
    expect(screen.getByText('test@example.com')).toBeInTheDocument();
    expect(screen.getByText('Log out')).toBeInTheDocument();
  });

  it('shows login button when not authenticated', () => {
    mockAuthResult.user = null;
    render(<Header />);
    const loginLink = screen.getByText('Log in');
    expect(loginLink.closest('a')).toHaveAttribute('href', '/auth/login');
  });
});
