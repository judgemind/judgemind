import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockSearchParams = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => mockSearchParams,
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

import ResetPasswordPage from '../page';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ResetPasswordPage', () => {
  beforeEach(() => {
    mockSearchParams = new URLSearchParams();
  });

  it('shows error when no token is provided', () => {
    render(<ResetPasswordPage />);
    expect(
      screen.getByText(/No reset token provided/),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Request a new link').closest('a'),
    ).toHaveAttribute('href', '/auth/forgot-password');
  });

  it('renders the reset form when token is present', () => {
    mockSearchParams = new URLSearchParams('token=abc123');
    render(<ResetPasswordPage />);
    expect(
      screen.getByText('Set a new password'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('New password')).toBeInTheDocument();
    expect(
      screen.getByLabelText('Confirm new password'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Reset password' }),
    ).toBeInTheDocument();
  });

  it('renders back to login link when token is present', () => {
    mockSearchParams = new URLSearchParams('token=abc123');
    render(<ResetPasswordPage />);
    expect(
      screen.getByText('Back to login').closest('a'),
    ).toHaveAttribute('href', '/auth/login');
  });

  it('shows error when passwords do not match', async () => {
    mockSearchParams = new URLSearchParams('token=abc123');
    render(<ResetPasswordPage />);
    fireEvent.change(screen.getByLabelText('New password'), {
      target: { value: 'password123' },
    });
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'different' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Reset password' }),
    );

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Passwords do not match',
      );
    });
  });

  it('shows error when password is too short', async () => {
    mockSearchParams = new URLSearchParams('token=abc123');
    render(<ResetPasswordPage />);
    fireEvent.change(screen.getByLabelText('New password'), {
      target: { value: 'short' },
    });
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'short' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Reset password' }),
    );

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Password must be at least 8 characters',
      );
    });
  });

  it('shows not-implemented error on valid submission', async () => {
    mockSearchParams = new URLSearchParams('token=abc123');
    render(<ResetPasswordPage />);
    fireEvent.change(screen.getByLabelText('New password'), {
      target: { value: 'newpassword123' },
    });
    fireEvent.change(screen.getByLabelText('Confirm new password'), {
      target: { value: 'newpassword123' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Reset password' }),
    );

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Password reset is not yet available',
      );
    });
  });
});
