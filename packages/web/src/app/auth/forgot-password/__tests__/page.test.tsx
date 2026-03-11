import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

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

import ForgotPasswordPage from '../page';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ForgotPasswordPage', () => {
  it('renders the forgot password form', () => {
    render(<ForgotPasswordPage />);
    expect(screen.getByText('Forgot password')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Send reset link' }),
    ).toBeInTheDocument();
  });

  it('renders back to login link', () => {
    render(<ForgotPasswordPage />);
    expect(
      screen.getByText('Back to login').closest('a'),
    ).toHaveAttribute('href', '/auth/login');
  });

  it('renders explanatory text', () => {
    render(<ForgotPasswordPage />);
    expect(
      screen.getByText(/send you a link to reset your password/),
    ).toBeInTheDocument();
  });

  it('shows success state after form submission', async () => {
    render(<ForgotPasswordPage />);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'test@example.com' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Send reset link' }),
    );

    await waitFor(() => {
      expect(screen.getByText('Check your email')).toBeInTheDocument();
    });
    expect(
      screen.getByText(/If that email is registered/),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Back to login').closest('a'),
    ).toHaveAttribute('href', '/auth/login');
  });
});
