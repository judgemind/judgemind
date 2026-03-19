'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useMutation, useQuery } from '@apollo/client';
import type { AuthUser } from '@/lib/auth-mutations';
import {
  ME_QUERY,
  LOGOUT_MUTATION,
  REFRESH_TOKEN_MUTATION,
} from '@/lib/auth-mutations';
import type { AuthPayload } from '@/lib/auth-mutations';
import { clearAccessToken, getAccessToken, setAccessToken } from '@/lib/auth-tokens';

interface AuthContextValue {
  /** The currently logged-in user, or null if not authenticated. */
  user: AuthUser | null;
  /** True while the initial auth check is in flight. */
  loading: boolean;
  /** Set the user after a successful login/register. */
  setUser: (user: AuthUser) => void;
  /** Log out: calls the mutation, clears local state. */
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  loading: true,
  setUser: () => {},
  logout: async () => {},
});

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

/**
 * Module-level singleton promise for the proactive token refresh.
 * Prevents duplicate refresh requests in React Strict Mode (which
 * double-fires effects in development) and avoids the race condition
 * where the API rotates the refresh token on first use, causing the
 * second concurrent request to fail with an invalid token.
 */
let inflightRefresh: Promise<unknown> | null = null;

/** @internal Reset module-level state between tests. */
export function _resetInflightRefresh(): void {
  inflightRefresh = null;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [refreshDone, setRefreshDone] = useState(false);

  // If the access token is already in memory (e.g. after login without a
  // page refresh), skip the refresh attempt and query `me` directly.
  // When no token is present (page refresh), the refresh mutation provides
  // user data directly — no need for a follow-up ME_QUERY.
  const hasTokenOnMount = useRef(getAccessToken() !== null);

  const skipMeQuery = !hasTokenOnMount.current;

  const { data, loading: meLoading } = useQuery<{ me: AuthUser | null }>(
    ME_QUERY,
    {
      fetchPolicy: 'network-only',
      skip: skipMeQuery,
    },
  );
  const [logoutMutation] = useMutation(LOGOUT_MUTATION);
  const [refreshTokenMutation] = useMutation<{ refreshToken: AuthPayload }>(
    REFRESH_TOKEN_MUTATION,
  );

  // On mount, if no access token exists, attempt a proactive refresh using
  // the HTTP-only refresh cookie. This covers the page-refresh case where
  // the in-memory token is lost but the cookie persists.
  //
  // Uses a module-level singleton promise to deduplicate the request across
  // React Strict Mode double-mounts.
  useEffect(() => {
    if (hasTokenOnMount.current) {
      // Token already in memory — no need for proactive refresh
      return;
    }

    if (!inflightRefresh) {
      inflightRefresh = refreshTokenMutation().finally(() => {
        inflightRefresh = null;
      });
    }

    let cancelled = false;

    void (async () => {
      try {
        const result = await (inflightRefresh as ReturnType<typeof refreshTokenMutation>);
        if (!cancelled && result?.data?.refreshToken) {
          setAccessToken(result.data.refreshToken.accessToken);
          setUser(result.data.refreshToken.user);
        }
      } catch {
        // No valid refresh cookie — user is genuinely not authenticated.
        // Fall through to unauthenticated state.
      }
      if (!cancelled) {
        setRefreshDone(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [refreshTokenMutation]);

  // When the ME_QUERY resolves (used when access token was already in memory),
  // update the user state.
  useEffect(() => {
    if (!meLoading && data) {
      setUser(data.me);
    }
  }, [data, meLoading]);

  const logout = useCallback(async () => {
    try {
      await logoutMutation();
    } catch {
      // Ignore logout errors — clear local state regardless
    }
    clearAccessToken();
    setUser(null);
  }, [logoutMutation]);

  // Loading is true while either the refresh attempt or the ME_QUERY is in flight.
  const loading = hasTokenOnMount.current ? meLoading : !refreshDone;

  const value = useMemo(
    () => ({ user, loading, setUser, logout }),
    [user, loading, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
