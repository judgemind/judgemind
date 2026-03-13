import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const { mockSetAccessToken } = vi.hoisted(() => ({
  mockSetAccessToken: vi.fn(),
}));
const mockSetUser = vi.fn();
let mockMutate: ReturnType<typeof vi.fn>;
let mockLoading: boolean;

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
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

vi.mock('@/lib/auth-tokens', () => ({
  setAccessToken: mockSetAccessToken,
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

import RegisterPage from '../page';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RegisterPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockLoading = false;
    mockMutate = vi.fn().mockResolvedValue({
      data: {
        register: {
          accessToken: 'test-token',
          user: {
            id: '1',
            email: 'new@example.com',
            emailVerified: false,
            displayName: null,
            role: 'user',
            createdAt: '2026-01-01',
          },
        },
      },
    });
  });

  it('renders the registration form', () => {
    render(<RegisterPage />);
    expect(screen.getByText('Create an account')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.getByLabelText('Confirm password')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Create account' }),
    ).toBeInTheDocument();
  });

  it('renders login link', () => {
    render(<RegisterPage />);
    expect(screen.getByText('Log in').closest('a')).toHaveAttribute(
      'href',
      '/auth/login',
    );
  });

  it('shows error when passwords do not match', async () => {
    render(<RegisterPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'new@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password123' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'different' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Passwords do not match',
      );
    });
    expect(mockMutate).not.toHaveBeenCalled();
  });

  it('shows error when password is too short', async () => {
    render(<RegisterPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'new@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'short' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'short' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Password must be at least 8 characters',
      );
    });
    expect(mockMutate).not.toHaveBeenCalled();
  });

  it('shows success state after successful registration', async () => {
    render(<RegisterPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'new@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password123' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'password123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(screen.getByText('Check your email')).toBeInTheDocument();
    });
    expect(screen.getByText('Go to login').closest('a')).toHaveAttribute(
      'href',
      '/auth/login',
    );
  });

  it('stores the access token on successful registration', async () => {
    render(<RegisterPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'new@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password123' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'password123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(mockSetAccessToken).toHaveBeenCalledWith('test-token');
    });
  });

  it('shows error on mutation failure', async () => {
    mockMutate = vi
      .fn()
      .mockRejectedValue(new Error('Email already registered'));
    render(<RegisterPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'existing@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password123' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'password123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Email already registered',
      );
    });
  });

  it('shows generic error for non-Error exceptions', async () => {
    mockMutate = vi.fn().mockRejectedValue('unexpected');
    render(<RegisterPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'test@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password123' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'password123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Registration failed. Please try again.',
      );
    });
  });
});
