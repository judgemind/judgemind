import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockToggle = vi.fn();
const mockLogout = vi.fn().mockResolvedValue(undefined);
let mockUser: { id: string; email: string } | null = null;
let mockLoading = false;

vi.mock('next/link', () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
    [key: string]: unknown;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ theme: 'light', toggle: mockToggle }),
}));

vi.mock('@/providers/AuthProvider', () => ({
  useAuth: () => ({
    user: mockUser,
    loading: mockLoading,
    setUser: vi.fn(),
    logout: mockLogout,
  }),
}));

import { Header } from '../Header';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Header', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUser = null;
    mockLoading = false;
  });

  it('renders the Judgemind logo', () => {
    render(<Header />);
    expect(screen.getByText('Judgemind')).toBeInTheDocument();
    expect(screen.getByText('Judgemind').closest('a')).toHaveAttribute(
      'href',
      '/',
    );
  });

  it('renders navigation links', () => {
    render(<Header />);
    expect(screen.getByText('Search')).toBeInTheDocument();
    expect(screen.getByText('Rulings')).toBeInTheDocument();
  });

  it('renders dark mode toggle', () => {
    render(<Header />);
    const toggleBtn = screen.getByLabelText('Toggle dark mode');
    expect(toggleBtn).toBeInTheDocument();
    fireEvent.click(toggleBtn);
    expect(mockToggle).toHaveBeenCalledOnce();
  });

  it('shows Log in link when not authenticated', () => {
    render(<Header />);
    const loginLink = screen.getByText('Log in');
    expect(loginLink.closest('a')).toHaveAttribute('href', '/auth/login');
  });

  it('shows Log out button when authenticated', () => {
    mockUser = { id: '1', email: 'test@example.com' };
    render(<Header />);
    expect(screen.getByText('Log out')).toBeInTheDocument();
    expect(screen.queryByText('Log in')).not.toBeInTheDocument();
  });

  it('calls logout when Log out is clicked', () => {
    mockUser = { id: '1', email: 'test@example.com' };
    render(<Header />);
    fireEvent.click(screen.getByText('Log out'));
    expect(mockLogout).toHaveBeenCalledOnce();
  });

  it('hides auth buttons while loading', () => {
    mockLoading = true;
    render(<Header />);
    expect(screen.queryByText('Log in')).not.toBeInTheDocument();
    expect(screen.queryByText('Log out')).not.toBeInTheDocument();
  });
});
