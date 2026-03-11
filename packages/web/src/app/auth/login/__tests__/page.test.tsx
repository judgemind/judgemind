import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockPush = vi.fn();
const mockSetUser = vi.fn();
let mockMutate: ReturnType<typeof vi.fn>;
let mockLoading: boolean;

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

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

vi.mock('@/providers/AuthProvider', () => ({
  useAuth: () => ({
    user: null,
    loading: false,
    setUser: mockSetUser,
    logout: vi.fn(),
  }),
}));

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useMutation: () => [mockMutate, { loading: mockLoading }],
  };
});

import LoginPage from '../page';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockLoading = false;
    mockMutate = vi.fn().mockResolvedValue({
      data: {
        login: {
          accessToken: 'test-token',
          user: {
            id: '1',
            email: 'test@example.com',
            emailVerified: true,
            displayName: 'Test',
            role: 'user',
            createdAt: '2026-01-01',
          },
        },
      },
    });
  });

  it('renders the login form with email and password fields', () => {
    render(<LoginPage />);
    expect(
      screen.getByText('Log in to your account'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Log in' }),
    ).toBeInTheDocument();
  });

  it('renders register and forgot password links', () => {
    render(<LoginPage />);
    expect(screen.getByText('Register').closest('a')).toHaveAttribute(
      'href',
      '/auth/register',
    );
    expect(
      screen.getByText('Forgot password?').closest('a'),
    ).toHaveAttribute('href', '/auth/forgot-password');
  });

  it('submits the form and redirects on success', async () => {
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'test@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Log in' }));

    await waitFor(() => {
      expect(mockMutate).toHaveBeenCalledWith({
        variables: { email: 'test@example.com', password: 'password123' },
      });
    });
    await waitFor(() => {
      expect(mockSetUser).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith('/');
    });
  });

  it('shows an error message on mutation failure', async () => {
    mockMutate = vi.fn().mockRejectedValue(new Error('Invalid credentials'));
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'bad@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'wrong' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Log in' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Invalid credentials',
      );
    });
  });

  it('shows a generic error for non-Error exceptions', async () => {
    mockMutate = vi.fn().mockRejectedValue('unexpected');
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'test@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Log in' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Login failed. Please try again.',
      );
    });
  });
});
