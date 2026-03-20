/**
 * Integration tests for the full authentication lifecycle.
 *
 * Unlike the existing unit tests that mock useAuth() entirely, these tests
 * use Apollo's MockedProvider to test the real component integration:
 * LoginPage -> AuthProvider -> Header, etc. This catches bugs where auth
 * state doesn't propagate correctly through the React tree.
 *
 * "e2e" here means end-to-end through the client-side auth stack, not
 * browser-level e2e (Playwright/Cypress). The GraphQL layer is mocked
 * at the network boundary via MockedProvider.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
} from '@testing-library/react';
import { MockedProvider, type MockedResponse } from '@apollo/client/testing';

// ---------------------------------------------------------------------------
// Mocks — only mock what's strictly needed (navigation, Next.js internals)
// ---------------------------------------------------------------------------

const mockPush = vi.fn();

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

vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ theme: 'light', toggle: vi.fn() }),
}));

// Mock recharts to avoid jsdom SVG rendering issues
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

// ---------------------------------------------------------------------------
// Imports — AFTER mocks
// ---------------------------------------------------------------------------

import { AuthProvider, useAuth } from '@/providers/AuthProvider';
import { Header } from '@/components/layout/Header';
import LoginPage from '@/app/auth/login/page';
import RegisterPage from '@/app/auth/register/page';
import { DataQualityDashboard } from '@/app/admin/data-quality/DataQualityDashboard';
import {
  LOGIN_MUTATION,
  REGISTER_MUTATION,
  LOGOUT_MUTATION,
  ME_QUERY,
} from '@/lib/auth-mutations';
import type { AuthUser } from '@/lib/auth-mutations';
import {
  DATA_QUALITY_OVERVIEW_QUERY,
  DATA_QUALITY_METRICS_QUERY,
} from '@/lib/data-quality-queries';
import { REFRESH_TOKEN_MUTATION } from '@/lib/auth-mutations';
import {
  setAccessToken,
  clearAccessToken,
  getAccessToken,
} from '@/lib/auth-tokens';
import { _resetInflightRefresh } from '@/providers/AuthProvider';

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

const testUser: AuthUser = {
  id: '1',
  email: 'test@example.com',
  emailVerified: true,
  displayName: 'Test User',
  role: 'user',
  createdAt: '2026-01-01T00:00:00Z',
};

const adminUser: AuthUser = {
  id: '2',
  email: 'admin@example.com',
  emailVerified: true,
  displayName: 'Admin User',
  role: 'admin',
  createdAt: '2026-01-01T00:00:00Z',
};

// ---------------------------------------------------------------------------
// Mock response builders
// ---------------------------------------------------------------------------

function meQueryMock(user: AuthUser | null): MockedResponse {
  return {
    request: { query: ME_QUERY },
    result: { data: { me: user } },
  };
}

function loginMutationMock(
  email: string,
  password: string,
  response: { accessToken: string; user: AuthUser },
): MockedResponse {
  return {
    request: {
      query: LOGIN_MUTATION,
      variables: { email, password },
    },
    result: {
      data: { login: response },
    },
  };
}

function registerMutationMock(
  email: string,
  password: string,
  response: { accessToken: string; user: AuthUser },
): MockedResponse {
  return {
    request: {
      query: REGISTER_MUTATION,
      variables: { email, password },
    },
    result: {
      data: { register: response },
    },
  };
}

function logoutMutationMock(): MockedResponse {
  return {
    request: { query: LOGOUT_MUTATION },
    result: { data: { logout: true } },
  };
}

function refreshTokenFailMock(): MockedResponse {
  return {
    request: { query: REFRESH_TOKEN_MUTATION },
    error: new Error('No refresh token'),
  };
}

function dataQualityOverviewMock(): MockedResponse {
  return {
    request: { query: DATA_QUALITY_OVERVIEW_QUERY },
    result: {
      data: {
        dataQualityOverview: [
          {
            county: 'Los Angeles',
            healthStatus: 'green',
            rulingCount24h: 42,
            fieldCompletenessPct: 95.5,
            scraperLastSuccessAgeHours: 1.2,
            lastUpdated: '2026-01-01T00:00:00Z',
          },
        ],
      },
    },
  };
}

function dataQualityMetricsMock(): MockedResponse {
  return {
    request: {
      query: DATA_QUALITY_METRICS_QUERY,
    },
    variableMatcher: () => true,
    result: {
      data: {
        dataQualityMetrics: {
          edges: [],
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
    },
  };
}

// ---------------------------------------------------------------------------
// Helper to render with all providers
// ---------------------------------------------------------------------------

function renderWithProviders(
  ui: React.ReactElement,
  mocks: MockedResponse[],
) {
  return render(
    <MockedProvider mocks={mocks}>
      <AuthProvider>{ui}</AuthProvider>
    </MockedProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Auth lifecycle integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAccessToken();
    _resetInflightRefresh();
  });

  // -------------------------------------------------------------------------
  // Login flow
  // -------------------------------------------------------------------------
  describe('Login flow', () => {
    it('login stores access token and reflects user in AuthProvider', async () => {
      const mocks = [
        refreshTokenFailMock(), // No existing session
        loginMutationMock('test@example.com', 'password123', {
          accessToken: 'jwt-login-token',
          user: testUser,
        }),
      ];

      renderWithProviders(<LoginPage />, mocks);

      // Wait for initial ME_QUERY to resolve (loading=false)
      await waitFor(() => {
        expect(
          screen.getByRole('button', { name: 'Log in' }),
        ).toBeInTheDocument();
      });

      // Fill in the form
      fireEvent.change(screen.getByLabelText('Email'), {
        target: { value: 'test@example.com' },
      });
      fireEvent.change(screen.getByLabelText('Password'), {
        target: { value: 'password123' },
      });

      // Submit
      fireEvent.click(screen.getByRole('button', { name: 'Log in' }));

      // Verify token was stored
      await waitFor(() => {
        expect(getAccessToken()).toBe('jwt-login-token');
      });

      // Verify navigation occurred
      await waitFor(() => {
        expect(mockPush).toHaveBeenCalledWith('/');
      });
    });

    it('failed login does not store token or set user', async () => {
      const mocks: MockedResponse[] = [
        refreshTokenFailMock(),
        {
          request: {
            query: LOGIN_MUTATION,
            variables: {
              email: 'bad@example.com',
              password: 'wrong',
            },
          },
          error: new Error('Invalid credentials'),
        },
      ];

      renderWithProviders(<LoginPage />, mocks);

      await waitFor(() => {
        expect(
          screen.getByRole('button', { name: 'Log in' }),
        ).toBeInTheDocument();
      });

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

      expect(getAccessToken()).toBeNull();
      expect(mockPush).not.toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // Logout flow
  // -------------------------------------------------------------------------
  describe('Logout flow', () => {
    it('logout clears token and Header shows "Log in"', async () => {
      setAccessToken('pre-existing-token');

      const mocks = [
        meQueryMock(testUser), // Initially logged in
        logoutMutationMock(),
      ];

      renderWithProviders(<Header />, mocks);

      // Wait for ME_QUERY to resolve — should show user menu (dropdown trigger)
      await waitFor(() => {
        expect(screen.getByLabelText('User menu')).toBeInTheDocument();
      });
      expect(screen.queryByText('Log in')).not.toBeInTheDocument();

      // Open the user dropdown (Radix needs pointer events, use userEvent)
      const user = (await import('@testing-library/user-event')).default.setup();
      await user.click(screen.getByLabelText('User menu'));
      await waitFor(() => {
        expect(screen.getByText('Log out')).toBeInTheDocument();
      });
      await act(async () => {
        await user.click(screen.getByText('Log out'));
      });

      // After logout, should show "Log in" and token should be cleared
      await waitFor(() => {
        expect(screen.getByText('Log in')).toBeInTheDocument();
      });
      expect(getAccessToken()).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // ME_QUERY initial load
  // -------------------------------------------------------------------------
  describe('AuthProvider initial load', () => {
    it('reflects authenticated user from ME_QUERY', async () => {
      setAccessToken('existing-token');
      const mocks = [meQueryMock(testUser)];

      function UserDisplay() {
        const { user, loading } = useAuth();
        if (loading) return <div>Loading...</div>;
        return <div data-testid="email">{user?.email ?? 'none'}</div>;
      }

      renderWithProviders(<UserDisplay />, mocks);

      await waitFor(() => {
        expect(screen.getByTestId('email')).toHaveTextContent(
          'test@example.com',
        );
      });
    });

    it('shows unauthenticated state when ME_QUERY returns null', async () => {
      const mocks = [refreshTokenFailMock()];

      renderWithProviders(<Header />, mocks);

      await waitFor(() => {
        expect(screen.getByText('Log in')).toBeInTheDocument();
      });
      expect(screen.queryByText('Log out')).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Registration flow
  // -------------------------------------------------------------------------
  describe('Registration flow', () => {
    it('successful registration stores token and shows success state', async () => {
      const newUser: AuthUser = {
        id: '3',
        email: 'new@example.com',
        emailVerified: false,
        displayName: null,
        role: 'user',
        createdAt: '2026-01-01T00:00:00Z',
      };

      const mocks = [
        refreshTokenFailMock(),
        registerMutationMock('new@example.com', 'securepass123', {
          accessToken: 'jwt-register-token',
          user: newUser,
        }),
      ];

      renderWithProviders(<RegisterPage />, mocks);

      await waitFor(() => {
        expect(
          screen.getByRole('button', { name: 'Create account' }),
        ).toBeInTheDocument();
      });

      fireEvent.change(screen.getByLabelText('Email'), {
        target: { value: 'new@example.com' },
      });
      fireEvent.change(screen.getByLabelText('Password'), {
        target: { value: 'securepass123' },
      });
      fireEvent.change(screen.getByLabelText('Confirm password'), {
        target: { value: 'securepass123' },
      });

      fireEvent.click(
        screen.getByRole('button', { name: 'Create account' }),
      );

      // Verify token was stored
      await waitFor(() => {
        expect(getAccessToken()).toBe('jwt-register-token');
      });

      // Verify success state
      await waitFor(() => {
        expect(screen.getByText('Check your email')).toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Auth-gated pages
  // -------------------------------------------------------------------------
  describe('Auth-gated pages', () => {
    it('DataQualityDashboard shows "Access Denied" for non-admin users', async () => {
      setAccessToken('test-token');
      const mocks = [meQueryMock(testUser)]; // testUser has role: 'user'

      renderWithProviders(<DataQualityDashboard />, mocks);

      await waitFor(() => {
        expect(screen.getByText('Access Denied')).toBeInTheDocument();
      });
      expect(
        screen.getByText(/restricted to admin users/i),
      ).toBeInTheDocument();
    });

    it('DataQualityDashboard shows "Access Denied" for unauthenticated users', async () => {
      const mocks = [refreshTokenFailMock()];

      renderWithProviders(<DataQualityDashboard />, mocks);

      await waitFor(() => {
        expect(screen.getByText('Access Denied')).toBeInTheDocument();
      });
    });

    it('DataQualityDashboard renders content for admin users', async () => {
      setAccessToken('admin-token');
      const mocks = [
        meQueryMock(adminUser),
        dataQualityOverviewMock(),
        dataQualityMetricsMock(),
      ];

      renderWithProviders(<DataQualityDashboard />, mocks);

      // Should not show access denied
      await waitFor(() => {
        expect(screen.queryByText('Access Denied')).not.toBeInTheDocument();
      });

      // Should show the dashboard content (county names from the overview mock)
      await waitFor(
        () => {
          expect(screen.getByText('Los Angeles')).toBeInTheDocument();
        },
        { timeout: 3000 },
      );
    });
  });
});
