import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, waitFor } from '@testing-library/react';
import { renderHook } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const { mockClearAccessToken, mockGetAccessToken, mockSetAccessToken } =
  vi.hoisted(() => ({
    mockClearAccessToken: vi.fn(),
    mockGetAccessToken: vi.fn().mockReturnValue(null),
    mockSetAccessToken: vi.fn(),
  }));

let mockQueryData: { me: import('@/lib/auth-mutations').AuthUser | null };
let mockQueryLoading: boolean;
let mockQuerySkip: boolean;
const mockLogoutMutate = vi.fn().mockResolvedValue({ data: { logout: true } });
const mockRefreshMutate = vi.fn().mockResolvedValue({ data: null });

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: (_doc: unknown, opts?: { skip?: boolean }) => {
      mockQuerySkip = opts?.skip ?? false;
      if (opts?.skip) {
        return { data: undefined, loading: false };
      }
      return { data: mockQueryData, loading: mockQueryLoading };
    },
    useMutation: (doc: { definitions?: Array<{ name?: { value?: string } }> }) => {
      const opName = doc?.definitions?.[0]?.name?.value ?? '';
      if (opName === 'Logout') {
        return [mockLogoutMutate, { loading: false }];
      }
      if (opName === 'RefreshToken') {
        return [mockRefreshMutate, { loading: false }];
      }
      // Fallback for unknown mutations
      return [vi.fn(), { loading: false }];
    },
  };
});

vi.mock('@/lib/auth-tokens', () => ({
  clearAccessToken: mockClearAccessToken,
  getAccessToken: mockGetAccessToken,
  setAccessToken: mockSetAccessToken,
}));

import { AuthProvider, useAuth, _resetInflightRefresh } from '../AuthProvider';

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
// Tests — existing behavior (access token already in memory)
// ---------------------------------------------------------------------------

describe('AuthProvider — token in memory (no refresh needed)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockQueryData = { me: null };
    mockQueryLoading = true;
    // Simulate access token already in memory (e.g. after login, no page refresh)
    mockGetAccessToken.mockReturnValue('existing-token');
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

  it('does not attempt a proactive refresh', async () => {
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
    expect(mockRefreshMutate).not.toHaveBeenCalled();
  });

  it('does not skip the ME_QUERY', () => {
    mockQueryLoading = false;
    mockQueryData = { me: testUser };
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );
    expect(mockQuerySkip).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Tests — page refresh (no access token, refresh cookie may exist)
// ---------------------------------------------------------------------------

describe('AuthProvider — page refresh (no access token)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetInflightRefresh();
    mockQueryData = { me: null };
    mockQueryLoading = true;
    // Simulate page refresh: no access token in memory
    mockGetAccessToken.mockReturnValue(null);
    mockRefreshMutate.mockResolvedValue({ data: null });
  });

  it('attempts a proactive refresh on mount when no access token', async () => {
    mockRefreshMutate.mockResolvedValue({
      data: {
        refreshToken: {
          accessToken: 'refreshed-token',
          user: testUser,
        },
      },
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(mockRefreshMutate).toHaveBeenCalledOnce();
    });
  });

  it('sets the user from refresh response on successful refresh', async () => {
    mockRefreshMutate.mockResolvedValue({
      data: {
        refreshToken: {
          accessToken: 'refreshed-token',
          user: testUser,
        },
      },
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });
    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });
  });

  it('stores the access token from refresh response', async () => {
    mockRefreshMutate.mockResolvedValue({
      data: {
        refreshToken: {
          accessToken: 'refreshed-token',
          user: testUser,
        },
      },
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(mockSetAccessToken).toHaveBeenCalledWith('refreshed-token');
    });
  });

  it('shows user as null when refresh fails (no valid cookie)', async () => {
    mockRefreshMutate.mockRejectedValue(new Error('No refresh token'));

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

  it('shows user as null when refresh returns no data', async () => {
    mockRefreshMutate.mockResolvedValue({ data: null });

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

  it('shows loading=true while refresh is in flight', () => {
    // Make refresh never resolve
    mockRefreshMutate.mockReturnValue(new Promise(() => {}));

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    expect(screen.getByTestId('loading').textContent).toBe('true');
  });

  it('skips the ME_QUERY when no token on mount', async () => {
    mockRefreshMutate.mockResolvedValue({
      data: {
        refreshToken: {
          accessToken: 'refreshed-token',
          user: testUser,
        },
      },
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    // ME_QUERY should be skipped — user data comes from refresh
    expect(mockQuerySkip).toBe(true);

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });
  });

  it('deduplicates refresh calls (Strict Mode safety)', async () => {
    mockRefreshMutate.mockResolvedValue({
      data: {
        refreshToken: {
          accessToken: 'refreshed-token',
          user: testUser,
        },
      },
    });

    // Render twice to simulate React Strict Mode double-mount
    const { unmount } = render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );
    unmount();
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
    });

    // refreshTokenMutation should only have been called once despite two mounts
    expect(mockRefreshMutate).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Tests — logout
// ---------------------------------------------------------------------------

describe('AuthProvider — logout', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockQueryData = { me: testUser };
    mockQueryLoading = false;
    // Token in memory for logout tests
    mockGetAccessToken.mockReturnValue('existing-token');
  });

  it('clears the user on logout', async () => {
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
});

// ---------------------------------------------------------------------------
// Tests — setUser
// ---------------------------------------------------------------------------

describe('AuthProvider — setUser', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockQueryData = { me: null };
    mockQueryLoading = false;
    mockGetAccessToken.mockReturnValue('existing-token');
  });

  it('allows setting the user via setUser', async () => {
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

// ---------------------------------------------------------------------------
// Tests — useAuth outside provider
// ---------------------------------------------------------------------------

describe('useAuth', () => {
  it('returns default values when used outside of AuthProvider', () => {
    const { result } = renderHook(() => useAuth());
    expect(result.current.user).toBeNull();
    expect(result.current.loading).toBe(true);
  });
});
