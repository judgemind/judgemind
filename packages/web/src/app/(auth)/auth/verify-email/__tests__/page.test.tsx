import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockSearchParams = new URLSearchParams();
let mockMutate: ReturnType<typeof vi.fn>;

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

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useMutation: () => [mockMutate],
  };
});

import VerifyEmailPage from '../page';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('VerifyEmailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParams = new URLSearchParams();
    mockMutate = vi
      .fn()
      .mockResolvedValue({ data: { verifyEmail: true } });
  });

  afterEach(() => {
    cleanup();
  });

  it('shows error when no token is provided', async () => {
    render(<VerifyEmailPage />);
    await waitFor(() => {
      expect(
        screen.getByText('No verification token provided.'),
      ).toBeInTheDocument();
    });
  });

  it('shows loading state initially when token is present', () => {
    mockSearchParams = new URLSearchParams('token=abc123');
    // Make the mutation hang to observe loading state
    mockMutate = vi.fn().mockReturnValue(new Promise(() => {}));
    render(<VerifyEmailPage />);
    expect(
      screen.getByText('Verifying your email…'),
    ).toBeInTheDocument();
  });

  it('shows success state after successful verification', async () => {
    mockSearchParams = new URLSearchParams('token=valid-token');
    mockMutate = vi
      .fn()
      .mockResolvedValue({ data: { verifyEmail: true } });
    render(<VerifyEmailPage />);

    await waitFor(() => {
      expect(
        screen.getByText(/Email verified/),
      ).toBeInTheDocument();
    });
    const loginLinks = screen.getAllByText('Go to login');
    expect(loginLinks[0].closest('a')).toHaveAttribute(
      'href',
      '/auth/login',
    );
  });

  it('shows error state when verification returns false', async () => {
    mockSearchParams = new URLSearchParams('token=expired-token');
    mockMutate = vi
      .fn()
      .mockResolvedValue({ data: { verifyEmail: false } });
    render(<VerifyEmailPage />);

    await waitFor(() => {
      expect(
        screen.getByText('Verification link is expired or invalid.'),
      ).toBeInTheDocument();
    });
  });

  it('shows error state when mutation throws', async () => {
    mockSearchParams = new URLSearchParams('token=bad-token');
    mockMutate = vi
      .fn()
      .mockRejectedValue(new Error('Token expired'));
    render(<VerifyEmailPage />);

    await waitFor(() => {
      expect(screen.getByText('Token expired')).toBeInTheDocument();
    });
  });

  it('shows generic error for non-Error exceptions', async () => {
    mockSearchParams = new URLSearchParams('token=bad-token');
    mockMutate = vi.fn().mockRejectedValue('unexpected');
    render(<VerifyEmailPage />);

    await waitFor(() => {
      expect(
        screen.getByText(
          'Verification link is expired or invalid.',
        ),
      ).toBeInTheDocument();
    });
  });
});
