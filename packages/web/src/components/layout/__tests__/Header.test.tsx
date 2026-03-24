import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockToggle = vi.fn();
const mockLogout = vi.fn().mockResolvedValue(undefined);
let mockUser: { id: string; email: string; displayName?: string | null } | null = null;
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

let mockPathname = '/search';
vi.mock('next/navigation', () => ({
  usePathname: () => mockPathname,
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
    mockPathname = '/search';
  });

  it('renders the wordmark logo', () => {
    render(<Header />);
    expect(screen.getByText('judge')).toBeInTheDocument();
    expect(screen.getByText('mind')).toBeInTheDocument();
    expect(screen.getByText('judge').closest('a')).toHaveAttribute(
      'href',
      '/',
    );
  });

  it('does not render duplicate nav links (sidebar owns navigation)', () => {
    render(<Header />);
    expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument();
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

  it('renders Log in button with outline variant', () => {
    render(<Header />);
    const loginLink = screen.getByText('Log in');
    // The outline variant button wrapping the link should not use the default/primary style
    const button = loginLink.closest('a');
    expect(button?.className).toContain('border');
    expect(button?.className).not.toContain('bg-primary');
  });

  it('shows user menu button when authenticated', () => {
    mockUser = { id: '1', email: 'test@example.com' };
    render(<Header />);
    expect(screen.getByLabelText('User menu')).toBeInTheDocument();
    expect(screen.queryByText('Log in')).not.toBeInTheDocument();
  });

  it('shows Log out in dropdown when user menu is clicked', async () => {
    const user = await import('@testing-library/user-event');
    const userEvent = user.default.setup();
    mockUser = { id: '1', email: 'test@example.com' };
    render(<Header />);
    await userEvent.click(screen.getByLabelText('User menu'));
    expect(screen.getByText('Log out')).toBeInTheDocument();
  });

  it('calls logout when Log out is clicked in dropdown', async () => {
    const user = await import('@testing-library/user-event');
    const userEvent = user.default.setup();
    mockUser = { id: '1', email: 'test@example.com' };
    render(<Header />);
    await userEvent.click(screen.getByLabelText('User menu'));
    await userEvent.click(screen.getByText('Log out'));
    expect(mockLogout).toHaveBeenCalledOnce();
  });

  it('hides auth buttons while loading', () => {
    mockLoading = true;
    render(<Header />);
    expect(screen.queryByText('Log in')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('User menu')).not.toBeInTheDocument();
  });

  it('renders hamburger menu on non-auth pages', () => {
    mockPathname = '/rulings';
    render(<Header />);
    expect(screen.getByLabelText('Toggle menu')).toBeInTheDocument();
  });

  it('hides hamburger menu on auth pages', () => {
    mockPathname = '/auth/login';
    render(<Header />);
    expect(screen.queryByLabelText('Toggle menu')).not.toBeInTheDocument();
  });

  it('hides hamburger menu on auth register page', () => {
    mockPathname = '/auth/register';
    render(<Header />);
    expect(screen.queryByLabelText('Toggle menu')).not.toBeInTheDocument();
  });
});
