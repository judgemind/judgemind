import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, waitFor } from '@testing-library/react';
import { renderHook } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const { mockClearAccessToken } = vi.hoisted(() => ({
  mockClearAccessToken: vi.fn(),
}));

let mockQueryData: { me: import('@/lib/auth-mutations').AuthUser | null };
let mockQueryLoading: boolean;
const mockLogoutMutate = vi.fn().mockResolvedValue({ data: { logout: true } });

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: () => ({ data: mockQueryData, loading: mockQueryLoading }),
    useMutation: () => [mockLogoutMutate, { loading: false }],
  };
});

vi.mock('@/lib/auth-tokens', () => ({
  clearAccessToken: mockClearAccessToken,
}));

import { AuthProvider, useAuth } from '../AuthProvider';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const testUser = {
  id: '1',
  email: 'test@example.com',
  emailVerified: true,
  displayName: 'Test User',
  role: 'user',
  createdAt: '2026-01-01',
};

function AuthConsumer() {
  const { user, loading, logout } = useAuth();
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="user">{user ? user.email : 'null'}</span>
      <button onClick={() => void logout()}>Logout</button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AuthProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockQueryData = { me: null };
    mockQueryLoading = true;
  });

  it('provides loading=true while the me query is in flight', () => {
    mockQueryLoading = true;
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );
    expect(screen.getByTestId('loading').textContent).toBe('true');
  });

  it('provides the user after the me query resolves', async () => {
    mockQueryLoading = false;
    mockQueryData = { me: testUser };
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });
  });

  it('provides user=null when me query returns null', async () => {
    mockQueryLoading = false;
    mockQueryData = { me: null };
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });
    expect(screen.getByTestId('user').textContent).toBe('null');
  });

  it('clears the user on logout', async () => {
    mockQueryLoading = false;
    mockQueryData = { me: testUser };
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });

    await act(async () => {
      screen.getByText('Logout').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('null');
    });
    expect(mockLogoutMutate).toHaveBeenCalledOnce();
  });

  it('clears the access token on logout', async () => {
    mockQueryLoading = false;
    mockQueryData = { me: testUser };
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });

    await act(async () => {
      screen.getByText('Logout').click();
    });

    await waitFor(() => {
      expect(mockClearAccessToken).toHaveBeenCalledOnce();
    });
  });

  it('clears user even when logout mutation throws', async () => {
    mockLogoutMutate.mockRejectedValueOnce(new Error('Network error'));
    mockQueryLoading = false;
    mockQueryData = { me: testUser };
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });

    await act(async () => {
      screen.getByText('Logout').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('null');
    });
  });

  it('clears access token even when logout mutation throws', async () => {
    mockLogoutMutate.mockRejectedValueOnce(new Error('Network error'));
    mockQueryLoading = false;
    mockQueryData = { me: testUser };
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });

    await act(async () => {
      screen.getByText('Logout').click();
    });

    await waitFor(() => {
      expect(mockClearAccessToken).toHaveBeenCalledOnce();
    });
  });

  it('allows setting the user via setUser', async () => {
    mockQueryLoading = false;
    mockQueryData = { me: null };

    function SetUserConsumer() {
      const { user, setUser } = useAuth();
      return (
        <div>
          <span data-testid="user">{user ? user.email : 'null'}</span>
          <button onClick={() => setUser(testUser)}>Set User</button>
        </div>
      );
    }

    render(
      <AuthProvider>
        <SetUserConsumer />
      </AuthProvider>,
    );

    expect(screen.getByTestId('user').textContent).toBe('null');

    await act(async () => {
      screen.getByText('Set User').click();
    });

    expect(screen.getByTestId('user').textContent).toBe('test@example.com');
  });
});

describe('useAuth', () => {
  it('returns default values when used outside of AuthProvider', () => {
    const { result } = renderHook(() => useAuth());
    expect(result.current.user).toBeNull();
    expect(result.current.loading).toBe(true);
  });
});
