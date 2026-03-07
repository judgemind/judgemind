'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { useMutation, useQuery } from '@apollo/client';
import type { AuthUser } from '@/lib/auth-mutations';
import { ME_QUERY, LOGOUT_MUTATION } from '@/lib/auth-mutations';

interface AuthContextValue {
  /** The currently logged-in user, or null if not authenticated. */
  user: AuthUser | null;
  /** True while the initial `me` query is in flight. */
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

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const { data, loading } = useQuery<{ me: AuthUser | null }>(ME_QUERY, {
    fetchPolicy: 'network-only',
  });
  const [logoutMutation] = useMutation(LOGOUT_MUTATION);

  useEffect(() => {
    if (!loading && data) {
      setUser(data.me);
    }
  }, [data, loading]);

  const logout = useCallback(async () => {
    try {
      await logoutMutation();
    } catch {
      // Ignore logout errors — clear local state regardless
    }
    setUser(null);
  }, [logoutMutation]);

  const value = useMemo(
    () => ({ user, loading, setUser, logout }),
    [user, loading, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
